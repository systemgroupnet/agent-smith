from flask import Flask, render_template, request, jsonify
from main import process_mr
import asyncio
import atexit
from dotenv import load_dotenv

# Load environment variables and setup proxy
load_dotenv()
from core.config import setup_proxy
setup_proxy()

# Import MCP session cleanup (centralized toolkit manager)
from core.MCP.toolkit_manager import close_mcp_toolkits
from core.config.langfuse_config import flush_langfuse

app = Flask(__name__,static_folder='assets', static_url_path='/assets')


async def cleanup_mcp_sessions():
    """Clean up MCP sessions on application shutdown."""
    print("\n🔌 Cleaning up MCP sessions...")
    try:
        await close_mcp_toolkits()
        print("✅ MCP sessions closed successfully")
    except Exception as e:
        print(f"⚠️  Error closing MCP sessions: {e}")


def cleanup_on_exit():
    """Cleanup handler for application exit."""
    try:
        asyncio.run(cleanup_mcp_sessions())
    except Exception as e:
        print(f"Warning: Could not clean up MCP sessions on exit: {e}")


# Register cleanup handler
atexit.register(cleanup_on_exit)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/review', methods=['POST'])
def review():
    data = request.get_json()
    mr = data.get('mr')
    
    if mr is None:
        return jsonify({'error': 'MR is required'}), 400
    
    try:
        asyncio.run(process_mr(mr))
        return jsonify({'result': 'DONE'})
    except Exception as e:
        print(f"Error processing MR: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        # Flush per request rather than only at shutdown: this server is
        # long-lived, so waiting for the batch interval (or exit) would leave a
        # just-finished review missing from Langfuse while someone is looking
        # for it. No-op when tracing is off.
        flush_langfuse()


if __name__ == '__main__':
    try:
        app.run(host='0.0.0.0', port=5000, debug=True)
    finally:
        cleanup_on_exit()