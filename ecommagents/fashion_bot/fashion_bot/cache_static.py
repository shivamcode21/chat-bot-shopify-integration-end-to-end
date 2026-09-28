"""
Cached Static Files Handler

Custom StaticFiles class that adds proper cache headers for CDN optimization.

Cache Strategy:
- chat-widget.js (loader): 5 minutes (fast rollouts)
- chat-widget.v*.js (bundles): 1 year, immutable (CDN edge caching)
- chat-widget-frame.html: 5 minutes (can update with new features)
- Other static files: 1 hour default
"""

import os
import re
from typing import Optional
from starlette.staticfiles import StaticFiles
from starlette.responses import Response, FileResponse
from starlette.types import Scope


class CachedStaticFiles(StaticFiles):
    """
    StaticFiles with intelligent cache headers based on file type.
    
    Implements industry-standard CDN caching:
    - Versioned files (chat-widget.v1.js) → immutable, 1 year
    - Loader (chat-widget.js) → short TTL, 5 minutes
    - HTML frames → short TTL, 5 minutes
    - Default → 1 hour
    """
    
    # Versioned bundle pattern: chat-widget.v1.js, chat-widget.v2.js, etc.
    VERSIONED_BUNDLE_PATTERN = re.compile(r'chat-widget\.v\d+\.js$')
    
    # Loader pattern: chat-widget.js (not versioned)
    LOADER_PATTERN = re.compile(r'chat-widget\.js$')
    
    # Frame HTML pattern
    FRAME_PATTERN = re.compile(r'chat-widget-frame\.html$')
    
    def get_cache_headers(self, path: str) -> dict:
        """
        Determine cache headers based on file path.
        
        Args:
            path: File path being requested
            
        Returns:
            Dictionary of cache headers to add
        """
        filename = os.path.basename(path)
        
        # Versioned bundles: 1 year, immutable (CDN will cache forever)
        if self.VERSIONED_BUNDLE_PATTERN.match(filename):
            return {
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Cache-Strategy": "versioned-bundle"
            }
        
        # Loader script: 5 minutes (allows fast version rollouts)
        if self.LOADER_PATTERN.match(filename):
            return {
                "Cache-Control": "public, max-age=300",
                "X-Cache-Strategy": "loader"
            }
        
        # Frame HTML: 5 minutes (can update with new features)
        if self.FRAME_PATTERN.match(filename):
            return {
                "Cache-Control": "public, max-age=300",
                "X-Cache-Strategy": "frame"
            }
        
        # Default: 1 hour for other static files
        return {
            "Cache-Control": "public, max-age=3600",
            "X-Cache-Strategy": "default"
        }
    
    async def get_response(self, path: str, scope: Scope) -> Response:
        """
        Override to add cache headers to response.
        """
        response = await super().get_response(path, scope)
        
        # Add cache headers if it's a successful response
        if response.status_code == 200:
            cache_headers = self.get_cache_headers(path)
            for key, value in cache_headers.items():
                response.headers[key] = value
        
        return response


def create_cached_static_files(directory: str, **kwargs) -> CachedStaticFiles:
    """
    Factory function to create CachedStaticFiles instance.
    
    Args:
        directory: Path to static files directory
        **kwargs: Additional arguments for StaticFiles
        
    Returns:
        CachedStaticFiles instance
    """
    return CachedStaticFiles(directory=directory, **kwargs)

