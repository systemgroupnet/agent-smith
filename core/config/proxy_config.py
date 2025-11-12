"""
Proxy configuration for OpenAI API calls.

This module sets up proxy support for all OpenAI API calls using environment variables.
Supports both authenticated and non-authenticated proxies.
"""

import os
from urllib.parse import quote


def setup_proxy():
    """
    Configure proxy settings for OpenAI API calls.
    
    Reads proxy configuration from environment variables:
    - HTTP_PROXY: HTTP proxy URL (e.g., http://proxy.example.com:8080)
    - HTTPS_PROXY: HTTPS proxy URL (e.g., http://proxy.example.com:8080)
    - PROXY_USERNAME: Username for proxy authentication (optional)
    - PROXY_PASSWORD: Password for proxy authentication (optional)
    
    If credentials are provided, they will be embedded in the proxy URL.
    The OpenAI SDK (via httpx) automatically respects these environment variables.
    """
    http_proxy = os.getenv("HTTP_PROXY")
    https_proxy = os.getenv("HTTPS_PROXY")
    proxy_username = os.getenv("PROXY_USERNAME")
    proxy_password = os.getenv("PROXY_PASSWORD")
    
    # If credentials are provided, update the proxy URLs with authentication
    if proxy_username and proxy_password:
        # URL-encode the credentials to handle special characters
        encoded_username = quote(proxy_username, safe='')
        encoded_password = quote(proxy_password, safe='')
        
        # Build authenticated proxy URL
        auth_string = f"{encoded_username}:{encoded_password}@"
        
        if http_proxy:
            # Insert credentials into the proxy URL
            if "://" in http_proxy:
                protocol, rest = http_proxy.split("://", 1)
                http_proxy_with_auth = f"{protocol}://{auth_string}{rest}"
                os.environ["HTTP_PROXY"] = http_proxy_with_auth
                print(f"✓ HTTP proxy configured with authentication")
            else:
                http_proxy_with_auth = f"http://{auth_string}{http_proxy}"
                os.environ["HTTP_PROXY"] = http_proxy_with_auth
                print(f"✓ HTTP proxy configured with authentication")
        
        if https_proxy:
            # Insert credentials into the proxy URL
            if "://" in https_proxy:
                protocol, rest = https_proxy.split("://", 1)
                https_proxy_with_auth = f"{protocol}://{auth_string}{rest}"
                os.environ["HTTPS_PROXY"] = https_proxy_with_auth
                print(f"✓ HTTPS proxy configured with authentication")
            else:
                https_proxy_with_auth = f"http://{auth_string}{https_proxy}"
                os.environ["HTTPS_PROXY"] = https_proxy_with_auth
                print(f"✓ HTTPS proxy configured with authentication")
    else:
        # No credentials provided, just use the proxy URLs as-is
        if http_proxy:
            print(f"✓ HTTP proxy configured: {http_proxy}")
        if https_proxy:
            print(f"✓ HTTPS proxy configured: {https_proxy}")
    
    # If no proxy is configured at all, that's fine - just continue without proxy
    if not http_proxy and not https_proxy:
        print("ℹ No proxy configured - using direct connection")
    
    return True

