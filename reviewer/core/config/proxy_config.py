"""
Proxy configuration for OpenAI API calls only.

This module sets up proxy support for OpenAI API calls using environment variables.
GitLab and Atlassian requests will NOT use the proxy (they use direct connections).
Supports both authenticated and non-authenticated proxies.
"""

import os
from urllib.parse import quote


def setup_proxy():
    """
    Configure proxy settings for OpenAI API calls only.
    
    Reads proxy configuration from environment variables:
    - HTTP_PROXY: HTTP proxy URL (e.g., http://proxy.example.com:8080)
    - HTTPS_PROXY: HTTPS proxy URL (e.g., http://proxy.example.com:8080)
    - PROXY_USERNAME: Username for proxy authentication (optional)
    - PROXY_PASSWORD: Password for proxy authentication (optional)
    
    If credentials are provided, they will be embedded in the proxy URL.
    The OpenAI SDK (via httpx) automatically respects HTTP_PROXY and HTTPS_PROXY.
    
    To prevent GitLab and Atlassian requests from using the proxy, we set NO_PROXY
    to exclude those domains.
    """
    http_proxy = os.getenv("HTTP_PROXY")
    https_proxy = os.getenv("HTTPS_PROXY")
    proxy_username = os.getenv("PROXY_USERNAME")
    proxy_password = os.getenv("PROXY_PASSWORD")
    
    # Get GitLab, Atlassian, and collector domains to exclude from proxy. The
    # collector is an internal service; without excluding it, an outbound
    # corporate proxy (HTTP_PROXY) would try to route the analytics POST through
    # itself and fail (non-fatal, but it silently loses the event).
    gitlab_url = os.getenv("GITLAB_URL", "")
    jira_url = os.getenv("JIRA_URL", "")
    confluence_url = os.getenv("CONFLUENCE_URL", "")
    collector_url = os.getenv("COLLECTOR_URL", "")

    # Build NO_PROXY list to exclude internal services from using the proxy
    from urllib.parse import urlparse
    no_proxy_domains = []
    for url in (gitlab_url, jira_url, confluence_url, collector_url):
        if url:
            # e.g. https://gitlab.example.com -> gitlab.example.com
            netloc = urlparse(url).netloc
            if netloc:
                no_proxy_domains.append(netloc)

    # Also add common localhost and internal domains
    no_proxy_domains.extend(["localhost", "127.0.0.1", ".local"])
    
    # Set NO_PROXY to exclude GitLab and Atlassian domains
    if no_proxy_domains:
        no_proxy_value = ",".join(no_proxy_domains)
        os.environ["NO_PROXY"] = no_proxy_value
        os.environ["no_proxy"] = no_proxy_value  # Some libraries use lowercase
        print(f"✓ NO_PROXY configured to exclude: {', '.join(no_proxy_domains)}")
    
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
                print(f"✓ HTTP proxy configured with authentication (OpenAI only)")
            else:
                http_proxy_with_auth = f"http://{auth_string}{http_proxy}"
                os.environ["HTTP_PROXY"] = http_proxy_with_auth
                print(f"✓ HTTP proxy configured with authentication (OpenAI only)")
        
        if https_proxy:
            # Insert credentials into the proxy URL
            if "://" in https_proxy:
                protocol, rest = https_proxy.split("://", 1)
                https_proxy_with_auth = f"{protocol}://{auth_string}{rest}"
                os.environ["HTTPS_PROXY"] = https_proxy_with_auth
                print(f"✓ HTTPS proxy configured with authentication (OpenAI only)")
            else:
                https_proxy_with_auth = f"http://{auth_string}{https_proxy}"
                os.environ["HTTPS_PROXY"] = https_proxy_with_auth
                print(f"✓ HTTPS proxy configured with authentication (OpenAI only)")
    else:
        # No credentials provided, just use the proxy URLs as-is
        if http_proxy:
            os.environ["HTTP_PROXY"] = http_proxy
            print(f"✓ HTTP proxy configured: {http_proxy} (OpenAI only)")
        if https_proxy:
            os.environ["HTTPS_PROXY"] = https_proxy
            print(f"✓ HTTPS proxy configured: {https_proxy} (OpenAI only)")
    
    # If no proxy is configured at all, that's fine - just continue without proxy
    if not http_proxy and not https_proxy:
        print("ℹ No proxy configured - using direct connection")
    
    return True

