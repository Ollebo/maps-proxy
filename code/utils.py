import hmac
import os
import shutil


CACHE_KEY = os.environ.get('CACHE_KEY')
# Shared with map-maker (MAPS_PROXY_PURGE_KEY). Unset = every purge/clear is
# refused rather than open.
PURGE_API_KEY = os.environ.get('PURGE_API_KEY', '')
FILE_CACHE = '/file_cache/'


def purge_key_ok(request):
    given = request.headers.get('X-Api-Key', '')
    return bool(PURGE_API_KEY) and hmac.compare_digest(given, PURGE_API_KEY)


def purge_prefix(bucket, prefix):
    """Remove cached copies of bucket/prefix*. Only whole-directory prefixes
    (ending in '/') under public/ are accepted."""
    if not prefix.startswith('public/') or not prefix.endswith('/') or '..' in prefix:
        return 0
    target = os.path.realpath(os.path.join(FILE_CACHE, bucket, prefix))
    root = os.path.realpath(os.path.join(FILE_CACHE, bucket))
    if not target.startswith(root + os.sep) or not os.path.isdir(target):
        return 0
    count = sum(len(files) for _, _, files in os.walk(target))
    shutil.rmtree(target, ignore_errors=True)
    print('purged {0} cached files under {1}'.format(count, target))
    return count

def cleanCache():
    folder = '/file_cache/'
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print('Failed to delete %s. Reason: %s' % (file_path, e))
    return "Cache cleand"
    