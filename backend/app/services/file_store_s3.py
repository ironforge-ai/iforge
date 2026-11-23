"""Narrow synchronous adapter around boto3's dynamically typed S3 client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


_BYTES_ADAPTER = TypeAdapter(bytes)
_STR_ADAPTER = TypeAdapter(str)


class S3ObjectStoreClient(Protocol):
    """Typed object operations consumed by the async file-store facade."""

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def presigned_get_url(self, key: str, expires_in_seconds: int) -> str: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def list_object_sizes(self, prefix: str) -> list[tuple[str, int]]: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def close(self) -> None: ...


class S3ObjectStoreError(RuntimeError):
    """Sanitized provider or protocol failure from the S3 adapter."""


class _S3ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    code: str = Field(alias="Code")


class _S3ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    error: _S3ErrorDetail = Field(alias="Error")


class _S3ResponseMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    http_status_code: int = Field(alias="HTTPStatusCode", ge=200, le=299)


class _S3OperationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    response_metadata: _S3ResponseMetadata = Field(alias="ResponseMetadata")


class _S3ObjectSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    key: str = Field(alias="Key")
    size: int = Field(alias="Size", default=0, ge=0)


class _S3ListObjectsPage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    contents: list[_S3ObjectSummary] = Field(alias="Contents", default_factory=list)


class _S3DeleteObjectsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    response_metadata: _S3ResponseMetadata = Field(alias="ResponseMetadata")
    errors: list[object] = Field(alias="Errors", default_factory=list)


class _S3GetObjectResponse(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="ignore",
        strict=True,
    )

    body: StreamingBody = Field(alias="Body")


class _Boto3S3ObjectStoreClient:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        force_path_style: bool,
    ) -> None:
        self._bucket = bucket
        self._client: S3Client
        # signature_version pinned to SigV4 so presigned GET URLs verify
        # against S3-compatible endpoints that reject legacy SigV2 signing.
        config = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path" if force_path_style else "auto"},
        )
        try:
            if access_key_id or secret_access_key:
                self._client = boto3.client(
                    "s3",
                    region_name=region or None,
                    endpoint_url=endpoint_url or None,
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    config=config,
                )
            else:
                self._client = boto3.client(
                    "s3",
                    region_name=region or None,
                    endpoint_url=endpoint_url or None,
                    config=config,
                )
        except BotoCoreError as exc:
            raise S3ObjectStoreError("S3 client initialization failed") from exc

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        try:
            if content_type:
                response = self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
            else:
                response = self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        except (BotoCoreError, ClientError) as exc:
            raise S3ObjectStoreError("S3 object write failed") from exc
        _validate_operation_response(response, operation="write")

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                raise FileNotFoundError(key) from exc
            raise S3ObjectStoreError("S3 object read failed") from exc
        except BotoCoreError as exc:
            raise S3ObjectStoreError("S3 object read failed") from exc
        # Own the stream before validating metadata so rejection also closes it.
        try:
            body = _S3GetObjectResponse.model_validate(response).body
        except ValidationError:
            raise S3ObjectStoreError("S3 returned an invalid object body") from None
        try:
            try:
                _S3OperationResponse.model_validate(response)
            except ValidationError:
                raise S3ObjectStoreError("S3 returned an invalid object body") from None
            try:
                raw_data: object = body.read()
            except BotoCoreError as exc:
                raise S3ObjectStoreError("S3 object stream failed") from exc
        finally:
            body.close()
        try:
            return _BYTES_ADAPTER.validate_python(raw_data, strict=True)
        except ValidationError:
            raise S3ObjectStoreError("S3 returned an invalid object body") from None

    def delete(self, key: str) -> None:
        try:
            response = self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise S3ObjectStoreError("S3 object delete failed") from exc
        _validate_operation_response(response, operation="delete")

    def exists(self, key: str) -> bool:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise S3ObjectStoreError("S3 object metadata check failed") from exc
        except BotoCoreError as exc:
            raise S3ObjectStoreError("S3 object metadata check failed") from exc
        _validate_operation_response(response, operation="metadata check")
        return True

    def presigned_get_url(self, key: str, expires_in_seconds: int) -> str:
        try:
            url: object = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in_seconds,
            )
        except (BotoCoreError, ClientError) as exc:
            raise S3ObjectStoreError("S3 presigned URL generation failed") from exc
        try:
            return _STR_ADAPTER.validate_python(url, strict=True)
        except ValidationError:
            raise S3ObjectStoreError("S3 returned an invalid presigned URL") from None

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys.extend(_validate_list_page(page))
        except (BotoCoreError, ClientError) as exc:
            raise S3ObjectStoreError("S3 object list failed") from exc
        return keys

    def list_object_sizes(self, prefix: str) -> list[tuple[str, int]]:
        """Every key under ``prefix`` with its size, so a caller can bound before it reads."""
        objects: list[tuple[str, int]] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                objects.extend((item.key, item.size) for item in _validate_list_page_items(page))
        except (BotoCoreError, ClientError) as exc:
            raise S3ObjectStoreError("S3 object list failed") from exc
        return objects

    def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                page_keys = _validate_list_page(page)
                if not page_keys:
                    continue
                response = self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": key} for key in page_keys]},
                )
                _validate_delete_objects_response(response)
                deleted += len(page_keys)
        except (BotoCoreError, ClientError) as exc:
            raise S3ObjectStoreError("S3 prefix delete failed") from exc
        return deleted

    def close(self) -> None:
        try:
            self._client.close()
        except BotoCoreError as exc:
            raise S3ObjectStoreError("S3 client close failed") from exc


def create_s3_object_store_client(
    *,
    bucket: str,
    region: str,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
    force_path_style: bool,
) -> S3ObjectStoreClient:
    return _Boto3S3ObjectStoreClient(
        bucket=bucket,
        region=region,
        endpoint_url=endpoint_url,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        force_path_style=force_path_style,
    )


def _is_not_found(exc: ClientError) -> bool:
    try:
        response = _S3ErrorResponse.model_validate(exc.response)
    except ValidationError:
        return False
    return response.error.code in {"404", "NoSuchKey", "NotFound"}


def _validate_operation_response(response: object, *, operation: str) -> None:
    try:
        _S3OperationResponse.model_validate(response)
    except ValidationError:
        raise S3ObjectStoreError(f"S3 returned an invalid {operation} response") from None


def _validate_list_page(page: object) -> list[str]:
    return [item.key for item in _validate_list_page_items(page)]


def _validate_list_page_items(page: object) -> list[_S3ObjectSummary]:
    try:
        validated = _S3ListObjectsPage.model_validate(page)
    except ValidationError:
        raise S3ObjectStoreError("S3 returned an invalid list response") from None
    return validated.contents


def _validate_delete_objects_response(response: object) -> None:
    try:
        validated = _S3DeleteObjectsResponse.model_validate(response)
    except ValidationError:
        raise S3ObjectStoreError("S3 returned an invalid prefix delete response") from None
    if validated.errors:
        # boto3 reports per-key failures inside a 200 DeleteObjects response and
        # never raises on them itself; surface them so callers never treat a
        # partial delete as success. Provider details are deliberately not echoed.
        raise S3ObjectStoreError(
            f"S3 prefix delete reported failed keys ({len(validated.errors)} in batch)"
        )
