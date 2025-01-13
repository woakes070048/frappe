# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. Check LICENSE

import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from functools import wraps

import frappe

_SITE_CACHE = defaultdict(dict)


def __generate_request_cache_key(args: tuple, kwargs: dict) -> int:
	"""Generate a key for the cache."""
	if not kwargs:
		return hash(args)
	return hash((args, frozenset(kwargs.items())))


def request_cache(func: Callable) -> Callable:
	"""
	Decorator to cache function calls mid-request.

	Cache is stored in `frappe.local.request_cache`.

	The cache only persists for the current request and is cleared when the request is over.

	The function is called just once per request with the same set of (kw)arguments.

	---
	Usage:
	```
	        from frappe.utils.caching import request_cache

	        @request_cache
	        def calculate_pi(num_terms=0):
	            import math, time

	            print(f"{num_terms = }")
	            time.sleep(10)
	            return math.pi

	        calculate_pi(10)  # will calculate value
	        calculate_pi(10)  # will return value from cache
	```
	"""

	@wraps(func)
	def wrapper(*args, **kwargs):
		_cache = getattr(frappe.local, "request_cache", None)
		if _cache is None:
			return func(*args, **kwargs)
		try:
			args_key = __generate_request_cache_key(args, kwargs)
		except Exception:
			return func(*args, **kwargs)

		try:
			return _cache[func][args_key]
		except KeyError:
			return_val = func(*args, **kwargs)
			_cache[func][args_key] = return_val
			return return_val

	return wrapper


def site_cache(ttl: int | None = None, maxsize: int | None = None) -> Callable:
	"""
	Decorator to cache method calls across requests.

	The cache is stored in `frappe.utils.caching._SITE_CACHE`.

	The cache persists on the parent process.

	It offers a light-weight cache for the current process without the additional
	overhead of serializing / deserializing Python objects.

	Note: This cache isn't shared among workers. If you need to share data across
	workers, use redis (frappe.cache API) instead.

	---
	Usage:
	```
	        from frappe.utils.caching import site_cache

	        @site_cache
	        def calculate_pi():
	            import math, time

	            precision = get_precision("Math Constant", "Pi") # depends on site data
	            return round(math.pi, precision)

	        calculate_pi(10) # will calculate value
	        calculate_pi(10) # will return value from cache
	        calculate_pi.clear_cache() # clear this function's cache for all sites
	        calculate_pi(10) # will calculate value
	```
	"""

	def time_cache_wrapper(func: Callable | None = None) -> Callable:
		func_key = f"{func.__module__}.{func.__name__}"

		def clear_cache():
			"""Clear cache for this function for all sites if not specified."""
			_SITE_CACHE[func_key].clear()

		func.clear_cache = clear_cache

		if ttl is not None and not callable(ttl):
			func.ttl = ttl
			func.expiration = time.monotonic() + func.ttl

		if maxsize is not None and not callable(maxsize):
			func.maxsize = maxsize

		@wraps(func)
		def site_cache_wrapper(*args, **kwargs):
			site = getattr(frappe.local, "site", None)
			if not site:
				return func(*args, **kwargs)

			arguments_key = f"{site}::{__generate_request_cache_key(args, kwargs)}"

			if hasattr(func, "ttl") and time.monotonic() >= func.expiration:
				func.clear_cache()
				func.expiration = time.monotonic() + func.ttl

			# NOTE: Important things to consider from thread safety POV:
			#   1. Other thread can issue clear_cache and delete entire dictionary.
			#   2. Other thread can pop the exact elemement we are reading if maxsize is hit.

			# NOTE: Keep a local reference to dictionary of interest so it doesn't get swapped
			function_cache = _SITE_CACHE[func_key]

			try:
				return function_cache[arguments_key]
			except (KeyError, RuntimeError):
				# NOTE: This is just a cache miss or dictionary was modified while reading it
				pass

			if hasattr(func, "maxsize") and len(function_cache) >= func.maxsize:
				# Note: This implements FIFO eviction policy
				with suppress(RuntimeError):
					function_cache.pop(next(iter(function_cache)), None)

			result = func(*args, **kwargs)
			function_cache[arguments_key] = result

			return result

		return site_cache_wrapper

	if callable(ttl):
		return time_cache_wrapper(ttl)

	return time_cache_wrapper


def redis_cache(ttl: int | None = 3600, user: str | bool | None = None, shared: bool = False) -> Callable:
	"""Decorator to cache method calls and its return values in Redis

	args:
	        ttl: time to expiry in seconds, defaults to 1 hour
	        user: `true` should cache be specific to session user.
	        shared: `true` should cache be shared across sites
	"""

	def wrapper(func: Callable | None = None) -> Callable:
		func_key = f"{func.__module__}.{func.__qualname__}"

		def clear_cache():
			frappe.cache.delete_keys(func_key)

		func.clear_cache = clear_cache
		func.ttl = ttl if not callable(ttl) else 3600

		@wraps(func)
		def redis_cache_wrapper(*args, **kwargs):
			func_call_key = func_key + "::" + str(__generate_request_cache_key(args, kwargs))
			cached_val = frappe.cache.get_value(func_call_key, user=user, shared=shared)
			if cached_val is not None:
				return cached_val

			# Edge Case: None can mean two things: cache miss or the result itself is `None`
			# RedisWrapper doesn't give us any way to handle this cleanly.
			if frappe.cache.exists(func_call_key, user=user, shared=shared):
				return None

			val = func(*args, **kwargs)
			ttl = getattr(func, "ttl", 3600)
			frappe.cache.set_value(func_call_key, val, expires_in_sec=ttl, user=user, shared=shared)
			return val

		return redis_cache_wrapper

	if callable(ttl):
		return wrapper(ttl)
	return wrapper
