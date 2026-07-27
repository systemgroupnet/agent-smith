"""
MCP Toolkit Manager - Centralized MCP Tool Management

This module provides a centralized manager for all MCP toolkits (GitLab, Atlassian).
It exposes MCP tools directly to LangChain agents without wrapper methods.

Usage:
    from core.MCP.toolkit_manager import get_mcp_toolkit_manager
    
    manager = get_mcp_toolkit_manager()
    await manager.initialize()
    tools = manager.get_all_tools()
    
    # Use tools with LangChain agents
"""

from __future__ import annotations

import functools
import os
from typing import List, Optional, Dict, Literal, Awaitable, Callable
import asyncio
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from contextlib import AsyncExitStack

# Transport types
TransportType = Literal["sse", "streamable-http"]


def _cocoindex_env(suffix: str, default: str) -> str:
    """Read an ``MCP_COCOINDEX_<suffix>`` var, falling back to the legacy name.

    The code index used to be referred to as "AIAND Source", so its settings were
    ``MCP_AIAND_ENABLED``/``_URL``/``_TRANSPORT``. Those names live outside this
    repo too (CI variables, runner env, operators' .env files), and silently
    ignoring them would be the worst kind of breakage: ``MCP_AIAND_ENABLED=false``
    would start connecting to a server the operator had switched off, and a
    ``MCP_AIAND_URL`` pointing at a private index would fall back to the public
    default. So the new name wins, the old one still works.
    """
    value = os.getenv(f"MCP_COCOINDEX_{suffix}")
    if value is not None:
        return value
    return os.getenv(f"MCP_AIAND_{suffix}", default)


def _wrap_tools_resilient(tools: List[BaseTool]) -> List[BaseTool]:
    """Make each MCP tool's call non-fatal to the review.

    Every MCP server has a single ClientSession shared by all concurrent file
    reviews. When the server drops that session (``McpError: Session terminated``
    — idle/restart/overload), an unwrapped tool call *raises*, and that exception
    propagates out of the agent loop and crashes the whole file review (and, once
    the session is dead, every remaining call in the run fails the same way). But
    MCP tools only supply *optional* extra context, so a dead server should
    degrade the review, not abort it. Here we wrap each tool's coroutine so a
    failure returns an error string the agent reads as a normal tool observation —
    it then finishes from the file content + diff it already has (or the tool-agent
    salvage path) instead of throwing. The message tells the model not to retry, so
    a dead session doesn't burn the agent's iteration budget hammering it.
    """
    wrapped: List[BaseTool] = []
    for tool in tools:
        original = getattr(tool, "coroutine", None)
        if original is None:
            wrapped.append(tool)
            continue

        # MCP adapter tools declare response_format="content_and_artifact", so
        # LangChain requires their result to be a (content, artifact) 2-tuple — a
        # bare string raises "a two-tuple ... is expected", which would itself crash
        # the review. Match the tool's declared shape in the error path too.
        response_format = getattr(tool, "response_format", "content")

        def _make(tool_name: str, call, resp_format: str):
            @functools.wraps(call)
            async def _resilient(*args, **kwargs):
                try:
                    return await call(*args, **kwargs)
                except asyncio.CancelledError:
                    raise  # never swallow cancellation (timeouts/shutdown)
                except Exception as e:  # noqa: BLE001 — degrade any MCP failure
                    msg = (
                        f"ERROR: the '{tool_name}' tool is unavailable right now "
                        f"({type(e).__name__}: {e}). Do not retry it; continue the "
                        f"review using the file content and diff already provided."
                    )
                    print(f"  ⚠️  [MCP] {msg}")
                    # Return the shape the tool's response_format requires.
                    if resp_format == "content_and_artifact":
                        return msg, None
                    return msg
            return _resilient

        try:
            tool.coroutine = _make(tool.name, original, response_format)
        except Exception as e:  # pragma: no cover — if the tool model is frozen
            print(f"  ⚠️  [MCP] Could not wrap tool {getattr(tool, 'name', '?')}: {e}")
        wrapped.append(tool)
    return wrapped


class MCPToolkitManager:
    """
    Centralized manager for all MCP toolkits.
    
    This class handles initialization and lifecycle management of multiple
    MCP toolkits (GitLab, Atlassian) and provides a unified interface to
    access all available MCP tools.
    """
    
    # Define allowed tools for each MCP server
    ALLOWED_GITLAB_TOOLS = {
        "get_file_contents",
        "get_repository_tree",
    }
    
    ALLOWED_ATLASSIAN_TOOLS = {
        "jira_get_issue",
        "confluence_get_page",
        "jira_search",
    }

    # cocoindex MCP (code index for old/unchanged files).
    # An empty set means "allow all tools" since the exact tool names exposed
    # by the cocoindex server are not fixed/known ahead of time.
    ALLOWED_COCOINDEX_TOOLS: set[str] = set()

    def __init__(self):
        """Initialize the toolkit manager."""
        self._gitlab_tools: Optional[List[BaseTool]] = None
        self._atlassian_tools: Optional[List[BaseTool]] = None
        self._cocoindex_tools: Optional[List[BaseTool]] = None
        self._exit_stack: Optional[AsyncExitStack] = None
        self._initialized = False
    
    @property
    def is_initialized(self) -> bool:
        """Check if manager is initialized."""
        return self._initialized
    
    def is_gitlab_enabled(self) -> bool:
        """Check if GitLab MCP integration is enabled."""
        return os.getenv("MCP_GITLAB_ENABLED", "true").lower() == "true"
    
    def is_atlassian_enabled(self) -> bool:
        """Check if Atlassian MCP integration is enabled."""
        return os.getenv("MCP_ATLASSIAN_ENABLED", "false").lower() == "true"

    def is_cocoindex_enabled(self) -> bool:
        """Check if cocoindex MCP integration is enabled."""
        return _cocoindex_env("ENABLED", "true").lower() == "true"

    def _get_gitlab_transport(self) -> TransportType:
        """Get GitLab MCP transport type from environment."""
        transport = os.getenv("MCP_GITLAB_TRANSPORT", "streamable-http").lower()
        if transport in ("sse", "streamable-http"):
            return transport  # type: ignore
        return "streamable-http"  # default to streamable-http
    
    def _get_atlassian_transport(self) -> TransportType:
        """Get Atlassian MCP transport type from environment."""
        transport = os.getenv("MCP_ATLASSIAN_TRANSPORT", "streamable-http").lower()
        if transport in ("sse", "streamable-http"):
            return transport  # type: ignore
        return "streamable-http"  # default to streamable-http

    def _get_cocoindex_transport(self) -> TransportType:
        """Get cocoindex MCP transport type from environment."""
        transport = _cocoindex_env("TRANSPORT", "streamable-http").lower()
        if transport in ("sse", "streamable-http"):
            return transport  # type: ignore
        return "streamable-http"  # default to streamable-http

    def _get_gitlab_url(self) -> str:
        """Get GitLab MCP server URL (no endpoint appended)."""
        return os.getenv("MCP_GITLAB_URL", "http://mcp-gitlab:3002").rstrip('/')

    def _get_atlassian_url(self) -> str:
        """Get Atlassian MCP server URL (no endpoint appended)."""
        return os.getenv("MCP_ATLASSIAN_URL", "http://mcp-atlassian:9000").rstrip('/')

    def _get_cocoindex_url(self) -> str:
        """Get cocoindex MCP server URL (code index for old/unchanged files)."""
        return _cocoindex_env(
            "URL",
            "http://internal.example.com/mcp",
        ).rstrip('/')
    
    async def initialize(self) -> None:
        """
        Initialize all enabled MCP toolkits.
        
        This method opens a session to each enabled MCP server (GitLab,
        Atlassian, cocoindex) and loads its tools via langchain-mcp-adapters.
        
        Raises:
            Exception: If toolkit initialization fails
        """
        if self._initialized:
            print("  ⚠️  [MCP Manager] Already initialized, skipping...")
            return
        
        print("\n" + "="*60)
        print("🔌 Initializing MCP Toolkits")
        print("="*60)
        
        # Create exit stack for managing async context managers
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        
        # Initialize GitLab toolkit (isolated to prevent cancel scope leakage)
        if self.is_gitlab_enabled():
            await self._initialize_toolkit_isolated(
                self._initialize_gitlab_tools,
                "GitLab MCP"
            )
        else:
            print("  ⏭️  [GitLab MCP] Disabled, skipping...")
        
        # Initialize Atlassian toolkit (isolated to prevent cancel scope leakage)
        if self.is_atlassian_enabled():
            await self._initialize_toolkit_isolated(
                self._initialize_atlassian_tools,
                "Atlassian MCP"
            )
        else:
            print("  ⏭️  [Atlassian MCP] Disabled, skipping...")

        # Initialize cocoindex toolkit (isolated to prevent cancel scope leakage)
        if self.is_cocoindex_enabled():
            await self._initialize_toolkit_isolated(
                self._initialize_cocoindex_tools,
                "cocoindex MCP"
            )
        else:
            print("  ⏭️  [cocoindex MCP] Disabled, skipping...")

        self._initialized = True
        print("\n✅ MCP Toolkit Manager initialized\n")
    
    async def _initialize_gitlab_tools(self) -> None:
        """Initialize GitLab MCP toolkit."""
        try:
            url = self._get_gitlab_url()
            transport = self._get_gitlab_transport()
            transport_name = "SSE (Server-Sent Events)" if transport == "sse" else "Streamable HTTP"
            
            print(f"  🔌 [GitLab MCP] Connecting to: {url}")
            print(f"     - Transport: {transport_name}")
            
            # Create client based on transport type
            if transport == "sse":
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    sse_client(url)
                )
            else:
                # streamable-http transport
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                    streamablehttp_client(url)
                )
            
            # Create ClientSession with the streams
            session = ClientSession(read_stream, write_stream)
            
            # Enter + initialize the session (kept open via the exit stack so the
            # loaded tools can call it), then load its tools (langchain-mcp-adapters).
            await self._exit_stack.enter_async_context(session)
            await session.initialize()

            print(f"  🔄 [GitLab MCP] Loading tools...")
            self._gitlab_tools = _wrap_tools_resilient(await load_mcp_tools(session))

            all_tools = self._gitlab_tools
            filtered_tools = [t for t in all_tools if t.name in self.ALLOWED_GITLAB_TOOLS]
            print(f"  ✅ [GitLab MCP] Initialized with {len(filtered_tools)}/{len(all_tools)} tool(s) (filtered)")
            
            # Print available tools (filtered)
            for tool in filtered_tools:
                print(f"     - {tool.name}: {tool.description[:80]}...")
                
        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            print(f"  ❌ [GitLab MCP] Initialization cancelled: {e}")
            self._gitlab_tools = None
            # Don't raise - allow partial initialization
        except Exception as e:
            print(f"  ❌ [GitLab MCP] Failed to initialize: {e}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()}")
            self._gitlab_tools = None
            # Don't raise - allow partial initialization
    
    async def _initialize_atlassian_tools(self) -> None:
        """Initialize Atlassian MCP toolkit."""
        try:
            url = self._get_atlassian_url()
            transport = self._get_atlassian_transport()
            transport_name = "SSE (Server-Sent Events)" if transport == "sse" else "Streamable HTTP"
            
            print(f"  🔌 [Atlassian MCP] Connecting to: {url}")
            print(f"     - Transport: {transport_name}")
            
            # Create client based on transport type
            if transport == "sse":
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    sse_client(url)
                )
            else:
                # streamable-http transport
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                    streamablehttp_client(url)
                )
            
            # Create ClientSession with the streams
            session = ClientSession(read_stream, write_stream)
            
            # Enter + initialize the session (kept open via the exit stack), then
            # load its tools (langchain-mcp-adapters).
            await self._exit_stack.enter_async_context(session)
            await session.initialize()

            print(f"  🔄 [Atlassian MCP] Loading tools...")
            self._atlassian_tools = _wrap_tools_resilient(await load_mcp_tools(session))

            all_tools = self._atlassian_tools
            filtered_tools = [t for t in all_tools if t.name in self.ALLOWED_ATLASSIAN_TOOLS]
            print(f"  ✅ [Atlassian MCP] Initialized with {len(filtered_tools)}/{len(all_tools)} tool(s) (filtered)")
            
            # Print available tools (filtered)
            for tool in filtered_tools:
                print(f"     - {tool.name}: {tool.description[:80]}...")
                
        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            print(f"  ❌ [Atlassian MCP] Initialization cancelled: {e}")
            self._atlassian_tools = None
            # Don't raise - allow partial initialization
        except Exception as e:
            print(f"  ❌ [Atlassian MCP] Failed to initialize: {e}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()}")
            self._atlassian_tools = None
            # Don't raise - allow partial initialization

    async def _initialize_cocoindex_tools(self) -> None:
        """Initialize cocoindex MCP toolkit (code index for old/unchanged files)."""
        try:
            url = self._get_cocoindex_url()
            transport = self._get_cocoindex_transport()
            transport_name = "SSE (Server-Sent Events)" if transport == "sse" else "Streamable HTTP"

            print(f"  🔌 [cocoindex MCP] Connecting to: {url}")
            print(f"     - Transport: {transport_name}")

            # Create client based on transport type
            if transport == "sse":
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    sse_client(url)
                )
            else:
                # streamable-http transport
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                    streamablehttp_client(url)
                )

            # Create ClientSession with the streams
            session = ClientSession(read_stream, write_stream)

            # Enter + initialize the session (kept open via the exit stack), then
            # load its tools (langchain-mcp-adapters).
            await self._exit_stack.enter_async_context(session)
            await session.initialize()

            print(f"  🔄 [cocoindex MCP] Loading tools...")
            self._cocoindex_tools = _wrap_tools_resilient(await load_mcp_tools(session))

            all_tools = self._cocoindex_tools
            filtered_tools = self._filter_cocoindex_tools(all_tools)
            print(f"  ✅ [cocoindex MCP] Initialized with {len(filtered_tools)}/{len(all_tools)} tool(s)")

            # Print available tools (filtered)
            for tool in filtered_tools:
                print(f"     - {tool.name}: {tool.description[:80]}...")

        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            print(f"  ❌ [cocoindex MCP] Initialization cancelled: {e}")
            self._cocoindex_tools = None
            # Don't raise - allow partial initialization
        except Exception as e:
            print(f"  ❌ [cocoindex MCP] Failed to initialize: {e}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()}")
            self._cocoindex_tools = None
            # Don't raise - allow partial initialization

    def _filter_cocoindex_tools(self, tools: List[BaseTool]) -> List[BaseTool]:
        """Filter cocoindex tools. Empty allow-set means expose all tools."""
        if not self.ALLOWED_COCOINDEX_TOOLS:
            return list(tools)
        return [tool for tool in tools if tool.name in self.ALLOWED_COCOINDEX_TOOLS]

    async def _initialize_toolkit_isolated(
        self,
        initializer: Callable[[], Awaitable[None]],
        toolkit_label: str,
        timeout: float = 30.0
    ) -> None:
        """Run toolkit initialization in a child task to avoid cancel leakage.
        
        Uses asyncio.shield() and timeout to fully isolate MCP cancel scopes
        from propagating to the main workflow.
        """
        async def _run_with_shield():
            """Inner wrapper to fully isolate the initializer."""
            try:
                await asyncio.wait_for(initializer(), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"  ❌ [{toolkit_label}] Initialization timed out after {timeout}s")
            except asyncio.CancelledError as e:
                # Suppress cancellation - don't let it propagate
                print(f"  ❌ [{toolkit_label}] Initialization cancelled: {e}")
            except Exception as e:
                print(f"  ❌ [{toolkit_label}] Initialization failed: {e}")
        
        task = asyncio.create_task(_run_with_shield())
        try:
            # Shield prevents external cancellation from reaching the task
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # If we get cancelled while shielding, wait for task to finish anyway
            # but don't propagate the cancellation
            try:
                await task
            except Exception:
                pass
            # Clear any pending cancellation on current task
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
        except Exception as e:
            print(f"  ❌ [{toolkit_label}] Unexpected error: {e}")
    
    def get_gitlab_tools(self) -> List[BaseTool]:
        """
        Get GitLab MCP tools (filtered to allowed tools only).
        
        Returns:
            List of GitLab MCP tools or empty list if not initialized
        """
        if self._gitlab_tools is None:
            return []
        
        try:
            all_tools = self._gitlab_tools
            # Filter to only allowed tools
            filtered_tools = [
                tool for tool in all_tools 
                if tool.name in self.ALLOWED_GITLAB_TOOLS
            ]
            return filtered_tools
        except Exception as e:
            print(f"  ⚠️  [GitLab MCP] Error getting tools: {e}")
            return []
    
    def get_atlassian_tools(self) -> List[BaseTool]:
        """
        Get Atlassian MCP tools (filtered to allowed tools only).
        
        Returns:
            List of Atlassian MCP tools or empty list if not initialized
        """
        if self._atlassian_tools is None:
            return []
        
        try:
            all_tools = self._atlassian_tools
            # Filter to only allowed tools
            filtered_tools = [
                tool for tool in all_tools 
                if tool.name in self.ALLOWED_ATLASSIAN_TOOLS
            ]
            return filtered_tools
        except Exception as e:
            print(f"  ⚠️  [Atlassian MCP] Error getting tools: {e}")
            return []

    def get_cocoindex_tools(self) -> List[BaseTool]:
        """
        Get cocoindex MCP tools (code index for old/unchanged files).

        Returns:
            List of cocoindex MCP tools or empty list if not initialized
        """
        if self._cocoindex_tools is None:
            return []

        try:
            all_tools = self._cocoindex_tools
            return self._filter_cocoindex_tools(all_tools)
        except Exception as e:
            print(f"  ⚠️  [cocoindex MCP] Error getting tools: {e}")
            return []

    def get_all_tools(self) -> List[BaseTool]:
        """
        Get all available MCP tools from all toolkits.

        Returns:
            Combined list of all MCP tools
        """
        all_tools = []
        all_tools.extend(self.get_gitlab_tools())
        all_tools.extend(self.get_atlassian_tools())
        all_tools.extend(self.get_cocoindex_tools())
        return all_tools

    def get_tools_by_category(self) -> Dict[str, List[BaseTool]]:
        """
        Get tools organized by category (gitlab, atlassian, cocoindex).

        Returns:
            Dictionary mapping category names to tool lists
        """
        return {
            "gitlab": self.get_gitlab_tools(),
            "atlassian": self.get_atlassian_tools(),
            "cocoindex": self.get_cocoindex_tools(),
        }
    
    async def close(self) -> None:
        """
        Close all MCP toolkits and cleanup resources.
        
        Should be called when shutting down the application.
        """
        print("\n🔌 Closing MCP Toolkits...")
        
        # Clear toolkit references first
        if self._gitlab_tools is not None:
            print("  ✅ [GitLab MCP] Toolkit closed")
            self._gitlab_tools = None
        
        if self._atlassian_tools is not None:
            print("  ✅ [Atlassian MCP] Toolkit closed")
            self._atlassian_tools = None

        if self._cocoindex_tools is not None:
            print("  ✅ [cocoindex MCP] Toolkit closed")
            self._cocoindex_tools = None

        # Close all async context managers (SSE/HTTP clients)
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except BaseExceptionGroup as eg:
                # Handle ExceptionGroup from TaskGroup (Python 3.11+)
                for exc in eg.exceptions:
                    exc_str = str(exc)
                    if "already running" in exc_str or "cancel scope" in exc_str or "TaskGroup" in exc_str:
                        print(f"  ⚠️  [MCP Manager] Async cleanup warning (safe to ignore): {type(exc).__name__}")
                    elif isinstance(exc, asyncio.CancelledError):
                        print("  ⚠️  [MCP Manager] Suppressed cancellation during cleanup")
                    else:
                        print(f"  ⚠️  [MCP Manager] Cleanup error: {exc}")
            except asyncio.CancelledError:
                # Suppress cancellation errors during cleanup
                print("  ⚠️  [MCP Manager] Suppressed cancellation during cleanup")
            except RuntimeError as e:
                # Handle "async generator already running" errors gracefully
                if "already running" in str(e) or "cancel scope" in str(e):
                    print(f"  ⚠️  [MCP Manager] Async cleanup warning (safe to ignore): {e}")
                else:
                    print(f"  ⚠️  [MCP Manager] Error closing exit stack: {e}")
            except Exception as e:
                # Catch-all for other exceptions
                e_str = str(e)
                if "TaskGroup" in e_str or "unhandled errors" in e_str:
                    print(f"  ⚠️  [MCP Manager] TaskGroup cleanup warning (safe to ignore)")
                else:
                    print(f"  ⚠️  [MCP Manager] Error closing exit stack: {e}")
            finally:
                self._exit_stack = None
        
        self._initialized = False
        print("✅ MCP Toolkit Manager closed\n")
    
    async def reset(self) -> None:
        """
        Reset the manager by closing and re-initializing all toolkits.
        
        Useful for recovering from errors or forcing a reconnection.
        """
        print("🔄 Resetting MCP Toolkit Manager...")
        await self.close()
        await self.initialize()


# =============================================================================
# Singleton Instance
# =============================================================================

_mcp_toolkit_manager: Optional[MCPToolkitManager] = None


def get_mcp_toolkit_manager() -> MCPToolkitManager:
    """
    Get or create the global MCP toolkit manager instance.
    
    Returns:
        Singleton MCPToolkitManager instance
    """
    global _mcp_toolkit_manager
    
    if _mcp_toolkit_manager is None:
        _mcp_toolkit_manager = MCPToolkitManager()
    
    return _mcp_toolkit_manager


async def initialize_mcp_toolkits() -> MCPToolkitManager:
    """
    Initialize MCP toolkits and return the manager.
    
    Convenience function that gets the manager and initializes it.
    
    Returns:
        Initialized MCPToolkitManager instance
    """
    manager = get_mcp_toolkit_manager()
    if not manager.is_initialized:
        await manager.initialize()
    return manager


async def close_mcp_toolkits() -> None:
    """
    Close all MCP toolkits.
    
    Convenience function for cleanup on application shutdown.
    """
    global _mcp_toolkit_manager
    
    if _mcp_toolkit_manager is not None:
        await _mcp_toolkit_manager.close()
        _mcp_toolkit_manager = None

