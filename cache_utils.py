from flask_caching import Cache

cache = Cache(config={
    'CACHE_TYPE': 'simple',
    'CACHE_DEFAULT_TIMEOUT': 300,  # Cache timeout in seconds (5 minutes)
    'CACHE_THRESHOLD': 1000,  # Maximum number of items to keep in the cache
    'CACHE_KEY_PREFIX': 'my_app_cache_'  # Prefix for cache keys
})
