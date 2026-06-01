from src.core.config import settings


def build_storage_url(path: str | None) -> str | None:
    if not path:
        return None

    base_url = settings.SUPABASE_URL.rstrip("/")
    bucket = settings.SUPABASE_STORAGE_BUCKET.strip("/")
    object_path = path.lstrip("/")
    return f"{base_url}/storage/v1/object/public/{bucket}/{object_path}"
