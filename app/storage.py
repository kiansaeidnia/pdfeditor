"""Where users' files live: a folder in DATA_DIR, or an S3-compatible bucket (AWS, Cloudflare R2, Backblaze, MinIO...)."""
import secrets
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

Failure = (OSError, BotoCoreError, ClientError)


class Local:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key  # keys are made of hex ids only, never of user input

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3:
    def __init__(self, bucket: str, endpoint: str = "", region: str = "", access_key: str = "", secret_key: str = ""):
        self.bucket = bucket
        self.client = boto3.client(
            "s3", endpoint_url=endpoint or None, region_name=region or "us-east-1",
            aws_access_key_id=access_key or None, aws_secret_access_key=secret_key or None,
            # path style works with every S3 look-alike; virtual hosts need DNS that most of them lack
            config=Config(s3={"addressing_style": "path"}, connect_timeout=10, read_timeout=60, retries={"max_attempts": 3}))

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def check(store) -> None:
    """Write, read back and delete a small object, so a wrong setting shows up during setup, not on first use."""
    key = f"pdfeditor-check-{secrets.token_hex(4)}"
    store.put(key, b"ok")
    try:
        if store.get(key) != b"ok":
            raise OSError("the test file came back different")
    finally:
        store.delete(key)
