import mimetypes
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class FileStorageError(RuntimeError):
    pass


class FileStorageUnavailable(FileStorageError):
    pass


class StoredFileNotFound(FileStorageError):
    pass


def _serverless_deployment():
    return (
        os.environ.get("VERCEL", "").strip().lower() not in {"", "0", "false"}
        or bool(os.environ.get("VERCEL_ENV", "").strip())
    )


def _local_root():
    configured = os.environ.get("UPLOAD_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return ROOT / "uploads"


def _s3_bucket():
    return os.environ.get("S3_BUCKET", "").strip()


def _storage_key(folder, filename):
    safe_folder = str(folder or "").strip().strip("/")
    safe_filename = os.path.basename(str(filename or "").strip())
    if not safe_folder or not safe_filename:
        raise FileStorageError("A storage folder and filename are required.")
    if safe_filename != str(filename or "").strip() or safe_filename in {".", ".."}:
        raise FileStorageError("Invalid stored filename.")
    return f"{safe_folder}/{safe_filename}"


def _content_type(filename):
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _max_file_bytes():
    try:
        configured = int(os.environ.get("MAX_UPLOAD_BYTES", str(4 * 1024 * 1024)))
    except ValueError:
        configured = 4 * 1024 * 1024
    return configured if configured > 0 else 4 * 1024 * 1024


def storage_status():
    if _s3_bucket():
        if not os.environ.get("AWS_ACCESS_KEY_ID", "").strip() or not os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip():
            return "misconfigured"
        return "s3"
    if _serverless_deployment():
        return "unconfigured"
    return "local"


@lru_cache(maxsize=1)
def _s3_client():
    if not _s3_bucket():
        return None
    if not os.environ.get("AWS_ACCESS_KEY_ID", "").strip() or not os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip():
        raise FileStorageUnavailable("S3 storage credentials are not configured.")
    try:
        import boto3
    except ImportError as exc:
        raise FileStorageUnavailable("boto3 is required for S3 storage.") from exc
    kwargs = {"region_name": os.environ.get("AWS_REGION", "us-east-1").strip() or "us-east-1"}
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def save_upload(file_storage, folder, filename, allowed_extensions):
    if not file_storage or not file_storage.filename:
        return ""
    extension = os.path.splitext(file_storage.filename)[1].lower()
    if extension not in {str(value).lower() for value in allowed_extensions}:
        raise ValueError("Unsupported file type.")
    data = file_storage.read()
    if not data:
        raise ValueError("The uploaded file is empty.")
    if len(data) > _max_file_bytes():
        raise ValueError("The uploaded file is too large.")

    key = _storage_key(folder, filename)
    client = _s3_client()
    if client is not None:
        args = {
            "Bucket": _s3_bucket(),
            "Key": key,
            "Body": data,
            "ContentType": _content_type(filename),
        }
        encryption = os.environ.get("S3_SERVER_SIDE_ENCRYPTION", "").strip().upper()
        if encryption:
            args["ServerSideEncryption"] = encryption
        try:
            client.put_object(**args)
        except Exception as exc:
            raise FileStorageError("Unable to store the uploaded file.") from exc
        return key

    if _serverless_deployment():
        raise FileStorageUnavailable("Durable upload storage is not configured on Vercel.")
    target = _local_root() / key
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(data)
    return key


def _s3_object_missing(exc):
    error = getattr(exc, "response", {}).get("Error", {})
    return str(error.get("Code", "")).lower() in {"404", "notfound", "nosuchkey"}


def load_upload(folder, filename):
    key = _storage_key(folder, filename)
    client = _s3_client()
    if client is not None:
        try:
            response = client.get_object(Bucket=_s3_bucket(), Key=key)
        except Exception as exc:
            if _s3_object_missing(exc):
                raise StoredFileNotFound("Stored file was not found.") from exc
            raise FileStorageError("Unable to retrieve the stored file.") from exc
        return response["Body"].read(), response.get("ContentType") or _content_type(filename)
    target = _local_root() / key
    if not target.is_file():
        raise StoredFileNotFound("Stored file was not found.")
    return target.read_bytes(), _content_type(filename)


def delete_upload(folder, filename):
    key = _storage_key(folder, filename)
    client = _s3_client()
    if client is not None:
        try:
            client.delete_object(Bucket=_s3_bucket(), Key=key)
        except Exception as exc:
            raise FileStorageError("Unable to delete the stored file.") from exc
        return
    target = _local_root() / key
    try:
        target.unlink()
    except FileNotFoundError:
        return
