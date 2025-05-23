from flask import Flask, request, jsonify, make_response
import requests
import os
import json
import logging
from waitress import serve
import subprocess
import time
import sys
from flask_cors import CORS
import time
import uuid
import random
import traceback
from cachetools import TTLCache
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================================
# CONFIGURATION SECTION
# ============================================================================

# Logging configuration - Customize log levels and outputs
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_RAW_DATA = os.environ.get("LOG_RAW_DATA", "1") == "1"  # Set to "0" to disable raw data logging
MAX_CHUNKS_TO_LOG = int(os.environ.get("MAX_CHUNKS_TO_LOG", "20"))  # Maximum number of chunks to log
LOG_TRUNCATE_LENGTH = int(os.environ.get("LOG_TRUNCATE_LENGTH", "1000"))  # Length to truncate logs

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("proxy.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Add a special logger for raw request/response data that only goes to console
raw_logger = logging.getLogger("raw_data")
raw_logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - RAW_DATA - %(message)s'))
raw_logger.addHandler(console_handler)
raw_logger.propagate = False  # Don't propagate to root logger

# ============================================================================
# AI PROVIDER CONFIGURATION
# ============================================================================

# AI Provider Selection - Set your preferred provider from these options:
# Options: "anthropic", "google", "groq", "grok", "ollama", "custom"
AI_PROVIDER = os.environ.get("AI_PROVIDER", "groq")

# API Keys - Set the appropriate API key for your chosen provider
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROK_API_KEY = os.environ.get("GROK_API_KEY", "")
CUSTOM_API_KEY = os.environ.get("CUSTOM_API_KEY", "")

# Provider base URLs - Modify as needed for custom endpoints
PROVIDER_URLS = {
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "groq": "https://api.groq.com/openai",
    "grok": "https://api.grok.ai",  # Replace with actual Grok API URL
    "ollama": "http://localhost:11434",  # Default local Ollama endpoint
    "custom": os.environ.get("CUSTOM_PROVIDER_URL", "")  # Your custom provider URL
}

# Provider chat endpoints - Modify as needed for custom endpoints
PROVIDER_CHAT_ENDPOINTS = {
    "anthropic": "/v1/messages",
    "google": "/v1/models/gemini-pro:generateContent",
    "groq": "/v1/chat/completions",
    "grok": "/v1/chat/completions",  # Replace with actual Grok endpoint
    "ollama": "/api/chat",
    "custom": os.environ.get("CUSTOM_PROVIDER_ENDPOINT", "")  # Your custom provider endpoint
}

# OpenAI API endpoints that we'll intercept
OPENAI_CHAT_ENDPOINT = "/v1/chat/completions"
CURSOR_CHAT_ENDPOINT = "/chat/completions"  # Additional endpoint for Cursor

# ============================================================================
# MODEL MAPPING CONFIGURATION
# ============================================================================

# These mappings allow you to use familiar model names (like OpenAI's)
# while routing to provider-specific models behind the scenes.
# 
# IMPORTANT: For Cursor integration, models should match what Cursor expects.
# Cursor will send requests with models like "gpt-4o" and we map these to
# the appropriate models for each provider.

# Model mapping - map OpenAI models to provider-specific models
MODEL_MAPPINGS = {
    "anthropic": {
        "gpt-4o": "claude-3-opus-20240229",
        "gpt-4o-2024-08-06": "claude-3-5-sonnet-20240620",
        "default": "claude-3-haiku-20240307",
        "gpt-3.5-turbo": "claude-3-haiku-20240307"
    },
    "google": {
        "gpt-4o": "gemini-1.5-pro-latest",
        "gpt-4o-2024-08-06": "gemini-1.5-pro-latest",
        "default": "gemini-1.5-flash-latest",
        "gpt-3.5-turbo": "gemini-1.5-flash-latest"
    },
    "groq": {
        "gpt-4o": "llama3-70b-8192",
        "gpt-4o-2024-08-06": "llama3-70b-8192",
        "default": "llama3-8b-8192",
        "gpt-3.5-turbo": "mixtral-8x7b-32768"
    },
    "grok": {
        "gpt-4o": "grok-3",
        "gpt-4o-2024-08-06": "grok-3",
        "default": "grok-3",
        "gpt-3.5-turbo": "grok-3"
    },
    "ollama": {
        "gpt-4o": "llama3",  # Adjust based on your locally available models
        "gpt-4o-2024-08-06": "llama3",
        "default": "llama3", 
        "gpt-3.5-turbo": "mistral"
    },
    "custom": {
        # Define your custom model mappings here
        "gpt-4o": os.environ.get("CUSTOM_MODEL_GPT4O", "your-best-model"),
        "gpt-4o-2024-08-06": os.environ.get("CUSTOM_MODEL_GPT4O_VERSION", "your-best-model"),
        "default": os.environ.get("CUSTOM_MODEL_DEFAULT", "your-default-model"),
        "gpt-3.5-turbo": os.environ.get("CUSTOM_MODEL_GPT35", "your-fast-model")
    }
}

# You can extend these mappings by adding your own custom models or updating the existing ones.
# Load custom model mappings from environment variables if defined
custom_model_env = os.environ.get("CUSTOM_MODEL_MAPPINGS", "{}")
try:
    custom_models = json.loads(custom_model_env)
    # Merge custom models with the default mappings
    for provider in custom_models:
        if provider in MODEL_MAPPINGS:
            MODEL_MAPPINGS[provider].update(custom_models[provider])
except json.JSONDecodeError:
    logger.warning("Failed to parse CUSTOM_MODEL_MAPPINGS environment variable. Using default mappings.")

# ============================================================================
# PERFORMANCE SETTINGS
# ============================================================================

# Create a TTL cache for request deduplication (5 second TTL)
request_cache = TTLCache(maxsize=1000, ttl=5)

# Initialize a cache for storing reasoning results, if enabled
r1_reasoning_cache = TTLCache(maxsize=100, ttl=1800)  # 30 minute TTL

# Add a streaming tracker to prevent multiple streaming for the same request
streaming_tracker = TTLCache(maxsize=100, ttl=10)  # 10 second TTL

# API request settings
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "120"))  # 120 seconds timeout for API calls
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))    # Maximum number of retries for failed requests

# ============================================================================
# SYSTEM PROMPT CONFIGURATION
# ============================================================================

# VERY IMPORTANT: System prompts tell the AI model how to behave in Cursor
# You can customize these system prompts to adjust how models work with Cursor
# Cursor expects certain behaviors, especially around tool usage

# Constants for agent mode - this helps the AI understand how to use tools in Cursor
AGENT_MODE_ENABLED = os.environ.get("AGENT_MODE_ENABLED", "1") == "1"

# The system prompt for agent mode - this provides instructions on how to use Cursor's tools
# You can customize this to adjust the AI's behavior when using tools
AGENT_INSTRUCTIONS = """
<tool_calling>
You have tools at your disposal to solve the coding task. Follow these rules regarding tool calls:
1. ALWAYS follow the tool call schema exactly as specified and provide all necessary parameters.
2. The conversation may reference tools that are no longer available. NEVER call tools that are not explicitly provided.
3. **NEVER refer to tool names when speaking to the USER.** For example, instead of saying 'I need to use the edit_file tool to edit your file', just say 'I will edit your file'.
4. Only calls tools when they are necessary. If the USER's task is general or you already know the answer, just respond without calling tools.
5. Before calling each tool, first explain to the USER why you are calling it.
6. NEVER recursively apply the same code block multiple times. If a code edit fails to apply correctly, try ONCE with a different approach or ask the user for guidance.
7. After attempting to edit a file, DO NOT repeat the same edit again if it doesn't work. Instead, explain the issue to the user and ask for guidance.
8. NEVER repeatedly attempt the same code edit multiple times. If an edit fails to apply after one retry, STOP and ask the user for guidance.
</tool_calling>

<making_code_changes>
When making code changes, NEVER output code to the USER, unless requested. Instead use one of the code edit tools to implement the change.
Use the code edit tools at most once per turn.
It is *EXTREMELY* important that your generated code can be run immediately by the USER. To ensure this, follow these instructions carefully:
1. Always group together edits to the same file in a single edit file tool call, instead of multiple smaller calls.
2. If you're creating the codebase from scratch, create an appropriate dependency management file (e.g. requirements.txt) with package versions and a helpful README.
3. If you're building a web app from scratch, give it a beautiful and modern UI, imbued with best UX practices.
4. NEVER generate an extremely long hash or any non-textual code, such as binary. These are not helpful to the USER and are very expensive.
5. Unless you are appending some small easy to apply edit to a file, or creating a new file, you MUST read the contents or section of what you're editing before editing it.
6. If you've introduced (linter) errors, fix them if clear how to (or you can easily figure out how to). Do not make uneducated guesses. And DO NOT loop more than 3 times on fixing linter errors on the same file. On the third time, you should stop and ask the user what to do next.
7. If you've suggested a reasonable code_edit that wasn't followed by the apply model, you should try reapplying the edit ONLY ONCE. If it still fails, explain the issue to the user and ask for guidance.
8. NEVER repeatedly attempt the same edit multiple times. If an edit fails to apply after one retry, STOP and ask the user for guidance.
</making_code_changes>

<searching_and_reading>
You have tools to search the codebase and read files. Follow these rules regarding tool calls:
1. If available, heavily prefer the semantic search tool to grep search, file search, and list dir tools.
2. If you need to read a file, prefer to read larger sections of the file at once over multiple smaller calls.
3. If you have found a reasonable place to edit or answer, do not continue calling tools. Edit or answer from the information you have found.
</searching_and_reading>

<preventing_recursion>
You must NEVER get stuck in a loop of repeatedly trying to apply the same code edit. If you notice that you're attempting to make the same edit more than once:
1. STOP immediately
2. Explain to the user that you were unable to apply the edit
3. Describe what you were trying to do
4. Ask the user for guidance on how to proceed
5. Wait for the user's response before taking any further action
</preventing_recursion>
"""

# Load custom agent instructions from an environment variable if defined
CUSTOM_AGENT_INSTRUCTIONS = os.environ.get("CUSTOM_AGENT_INSTRUCTIONS", "")
if CUSTOM_AGENT_INSTRUCTIONS:
    AGENT_INSTRUCTIONS = CUSTOM_AGENT_INSTRUCTIONS

# ============================================================================
# RECURSIVE EDIT PROTECTION
# ============================================================================

# Initialize a cache to track recent code edits (key: hash of edit, value: count)
code_edit_cache = TTLCache(maxsize=100, ttl=300)  # 5 minute TTL

# Track consecutive edits to the same file
file_edit_counter = TTLCache(maxsize=50, ttl=600)  # 10 minutes TTL
MAX_CONSECUTIVE_EDITS = int(os.environ.get("MAX_CONSECUTIVE_EDITS", "3"))  # Maximum consecutive edits to the same file

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def log_raw_data(title, data, truncate=LOG_TRUNCATE_LENGTH):
    """Log raw data with clear formatting and optional truncation"""
    # Skip logging if raw data logging is disabled
    if not LOG_RAW_DATA:
        return
        
    try:
        if isinstance(data, dict) or isinstance(data, list):
            formatted_data = json.dumps(data, indent=2)
        else:
            formatted_data = str(data)
        
        if truncate and len(formatted_data) > truncate:
            formatted_data = formatted_data[:truncate] + f"... [truncated, total length: {len(formatted_data)}]"
        
        separator = "=" * 40
        raw_logger.info(f"\n{separator}\n{title}\n{separator}\n{formatted_data}\n{separator}")
    except Exception as e:
        raw_logger.error(f"Error logging raw data: {str(e)}")

def collect_streaming_chunks(chunks, max_chunks=MAX_CHUNKS_TO_LOG):
    """
    Collect streaming chunks into a single string for logging
    
    Parameters:
    chunks (list): List of streaming chunks
    max_chunks (int): Maximum number of chunks to include
    
    Returns:
    str: A formatted string with all chunks
    """
    if not chunks:
        return "No chunks collected"
    
    # Limit the number of chunks to avoid excessive logging
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        truncated_message = f"\n... [truncated, {len(chunks) - max_chunks} more chunks]"
    else:
        truncated_message = ""
    
    # Format the chunks
    formatted_chunks = []
    for i, chunk in enumerate(chunks):
        formatted_chunks.append(f"Chunk {i+1}:\n{chunk}")
    
    return "\n\n".join(formatted_chunks) + truncated_message

def get_provider_api_key():
    """Get the API key for the currently selected provider"""
    if AI_PROVIDER == "anthropic":
        return ANTHROPIC_API_KEY
    elif AI_PROVIDER == "google":
        return GOOGLE_API_KEY
    elif AI_PROVIDER == "groq":
        return GROQ_API_KEY
    elif AI_PROVIDER == "grok":
        return GROK_API_KEY
    elif AI_PROVIDER == "ollama":
        return ""  # Ollama doesn't typically need an API key for local deployments
    elif AI_PROVIDER == "custom":
        return CUSTOM_API_KEY
    else:
        return None

def get_provider_url_and_endpoint():
    """Get the base URL and endpoint for the current provider"""
    base_url = PROVIDER_URLS.get(AI_PROVIDER, "")
    endpoint = PROVIDER_CHAT_ENDPOINTS.get(AI_PROVIDER, "")
    return base_url, endpoint

def get_provider_auth_headers():
    """Get the authentication headers for the current provider"""
    provider = AI_PROVIDER
    api_key = get_provider_api_key()
    
    if provider == "anthropic":
        return {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
    elif provider == "google":
        # Google might use a different auth mechanism
        return {
            "Content-Type": "application/json"
        }
    elif provider == "ollama":
        return {
            "Content-Type": "application/json"
        }
    elif provider in ["groq", "grok", "custom"]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
    else:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

# ============================================================================
# FORMAT CONVERSION FUNCTIONS
# ============================================================================

def format_request_for_provider(request_data):
    """
    Format the request data for the specific provider
    
    This function converts from OpenAI-compatible format to provider-specific formats.
    Modify this function if you need to support additional providers or custom formats.
    """
    provider = AI_PROVIDER
    formatted_data = request_data.copy()
    
    # Map the model name to the provider-specific model
    if 'model' in formatted_data and formatted_data['model'] in MODEL_MAPPINGS[provider]:
        formatted_data['model'] = MODEL_MAPPINGS[provider][formatted_data['model']]
    else:
        formatted_data['model'] = MODEL_MAPPINGS[provider]["default"]
    
    # Provider-specific transformations
    if provider == "anthropic":
        # Transform to Anthropic format
        if 'messages' in formatted_data:
            # No need to modify messages for Anthropic as they use a similar format
            # But we do need to ensure 'max_tokens' is set
            if 'max_tokens' not in formatted_data:
                formatted_data['max_tokens'] = 4096
    
    elif provider == "google":
        # Transform to Google format
        if 'messages' in formatted_data:
            # Google Gemini uses a different format
            contents = []
            for message in formatted_data['messages']:
                role = message.get('role', 'user')
                content = message.get('content', '')
                
                if role == 'system':
                    # System messages are handled differently in Gemini
                    # For simplicity, we'll add them as user messages with special prefix
                    contents.append({
                        "role": "user",
                        "parts": [{"text": f"[SYSTEM INSTRUCTION] {content}"}]
                    })
                else:
                    contents.append({
                        "role": "user" if role == "user" else "model",
                        "parts": [{"text": content}]
                    })
            
            # Replace messages with Google format
            formatted_data = {
                "contents": contents,
                "generationConfig": {
                    "temperature": formatted_data.get("temperature", 0.7),
                    "maxOutputTokens": formatted_data.get("max_tokens", 4096),
                    "topP": formatted_data.get("top_p", 0.95)
                }
            }
    
    elif provider == "ollama":
        # Transform to Ollama format
        if 'messages' in formatted_data:
            # Ollama has a format similar to OpenAI but with slight differences
            ollama_request = {
                "model": formatted_data['model'],
                "messages": formatted_data['messages'],
                "stream": formatted_data.get('stream', True),
                "options": {
                    "temperature": formatted_data.get('temperature', 0.7),
                    "top_p": formatted_data.get('top_p', 0.95)
                }
            }
            formatted_data = ollama_request
    
    # For Groq, Grok, and other OpenAI-compatible APIs, the format is already compatible
    
    return formatted_data

def format_response_for_openai(provider_response, original_model):
    """
    Format provider response to match OpenAI format
    
    This function converts from provider-specific response formats to OpenAI-compatible format.
    Modify this function if you need to support additional providers or custom formats.
    """
    provider = AI_PROVIDER
    
    try:
        if provider == "anthropic":
            # Convert Anthropic response to OpenAI format
            content = provider_response.get("content", [])
            content_text = ""
            for item in content:
                if item.get("type") == "text":
                    content_text += item.get("text", "")
            
            return {
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": original_model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content_text
                    },
                    "finish_reason": "stop"
                }],
                "usage": provider_response.get("usage", {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                })
            }
            
        elif provider == "google":
            # Convert Google response to OpenAI format
            content = ""
            if "candidates" in provider_response:
                candidate = provider_response["candidates"][0]
                if "content" in candidate and "parts" in candidate["content"]:
                    for part in candidate["content"]["parts"]:
                        if "text" in part:
                            content += part["text"]
            
            return {
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": original_model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }
            
        elif provider == "ollama":
            # Convert Ollama response to OpenAI format
            message = provider_response.get("message", {})
            content = message.get("content", "")
            
            return {
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": original_model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": "stop"
                }],
                "usage": provider_response.get("usage", {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                })
            }
            
        elif provider in ["groq", "grok", "custom"]:
            # Groq, Grok, and other OpenAI-compatible APIs already return OpenAI-compatible format
            response = provider_response.copy()
            response["model"] = original_model
            return response
        
        else:
            # Default case - create a basic response
            logger.warning(f"Unknown provider: {provider}, using default response format")
            return {
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": original_model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Response from unknown provider"
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }
            
    except Exception as e:
        logger.error(f"Error formatting response: {str(e)}")
        logger.error(traceback.format_exc())
        
        # Return a basic response structure
        return {
            "object": "chat.completion",
            "created": int(time.time()),
            "model": original_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Error formatting response: {str(e)}"
                    },
                    "finish_reason": "stop"
                }
            ]
        }

# ============================================================================
# FLASK APPLICATION SETUP
# ============================================================================

app = Flask(__name__)

# Enable CORS for all routes and origins with more permissive settings
CORS(app, 
     resources={r"/*": {
         "origins": "*",
         "allow_headers": ["Content-Type", "Authorization", "X-Requested-With", "Accept", "Origin"],
         "expose_headers": ["X-Request-ID", "openai-organization", "openai-processing-ms", "openai-version"],
         "methods": ["GET", "POST", "OPTIONS", "PUT", "DELETE"]
     }}
)

@app.after_request
def after_request(response):
    """Add CORS headers to all responses"""
    # Add CORS headers to every response
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With, Accept, Origin')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, PUT, DELETE')
    response.headers.add('Access-Control-Expose-Headers', 'X-Request-ID, openai-organization, openai-processing-ms, openai-version')
    response.headers.add('Access-Control-Max-Age', '86400')  # 24 hours
    
    # Only log response status and minimal headers for reduced verbosity
    if response.status_code != 200:
        logger.info(f"Response status: {response.status}")
    
    # Log raw response data only for non-streaming responses
    try:
        content_type = response.headers.get('Content-Type', '')
        if 'text/event-stream' not in content_type:
            response_data = response.get_data(as_text=True)
            # Only log if it's not too large
            if len(response_data) < 5000:
                log_raw_data(f"RESPONSE (Status: {response.status_code})", response_data)
            else:
                # Just log a summary for large responses
                log_raw_data(f"RESPONSE (Status: {response.status_code})", 
                            f"Large response ({len(response_data)} bytes) with content type: {content_type}")
    except Exception as e:
        raw_logger.error(f"Error logging response: {str(e)}")
    
    return response

@app.route('/debug', methods=['GET'])
def debug():
    """Return debug information and configuration status"""
    return jsonify({
        "status": "running",
        "provider": AI_PROVIDER,
        "endpoints": [
            "/v1/chat/completions",
            "/chat/completions",
            "/<path>/chat/completions",
            "/direct",
            "/simple",
            "/agent"
        ],
        "models": list(MODEL_MAPPINGS[AI_PROVIDER].keys()),
        "api_key_set": bool(get_provider_api_key()),
        "agent_mode_enabled": AGENT_MODE_ENABLED,
        "base_url": PROVIDER_URLS.get(AI_PROVIDER, ""),
        "chat_endpoint": PROVIDER_CHAT_ENDPOINTS.get(AI_PROVIDER, "")
    })

# Handle OPTIONS requests for all routes
@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    """Handle OPTIONS requests for all routes"""
    logger.info(f"OPTIONS request: /{path}")
    
    # Create a response with all the necessary CORS headers
    response = make_response('')
    response.status_code = 200
    
    # Add all the headers that might be expected
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, PUT, DELETE')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With, Accept, Origin')
    response.headers.add('Access-Control-Expose-Headers', 'X-Request-ID, openai-organization, openai-processing-ms, openai-version')
    response.headers.add('Access-Control-Max-Age', '86400')  # 24 hours
    
    return response

# ============================================================================
# MAIN REQUEST PROCESSING
# ============================================================================

def process_chat_request():
    """Process a chat completion request from any endpoint"""
    try:
        # Get client IP (for logging purposes)
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = request.headers.get('User-Agent', 'Unknown')
        
        # Log the request info but less verbosely
        logger.info(f"Request from {client_ip} using {user_agent.split(' ')[0]}")
        
        # Generate a unique request ID
        request_id = str(uuid.uuid4())
        
        # Check if we're already streaming this request
        request_hash = hash(str(request.data))
        if request_hash in streaming_tracker:
            logger.warning(f"Detected duplicate streaming request (hash: {request_hash}). Returning empty response.")
            # Return a simple response to prevent recursive streaming
            return jsonify({
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "default-model",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Request already being processed. Please wait for the response."
                    },
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            })
        
        # Mark this request as being streamed
        streaming_tracker[request_hash] = True
        
        # Log raw request data
        log_raw_data("REQUEST HEADERS", dict(request.headers))
        
        if request.is_json:
            log_raw_data("REQUEST JSON BODY", request.json)
        else:
            log_raw_data("REQUEST RAW BODY", request.data.decode('utf-8', errors='replace'))
        
        # Handle preflight OPTIONS request
        if request.method == 'OPTIONS':
            logger.info("OPTIONS preflight request")
            return handle_options(request.path.lstrip('/'))
        
        # Get the request data
        if request.is_json:
            data = request.json
            # Log message count and types without full content
            if 'messages' in data:
                messages = data['messages']
                msg_summary = [f"{m.get('role', 'unknown')}: {len(m.get('content', ''))}" for m in messages]
                logger.info(f"Processing {len(messages)} messages: {msg_summary}")
                
                # Take only the last few messages if there are too many
                if len(messages) > 10:
                    logger.info(f"Truncating message history from {len(messages)} to last 10 messages")
                    # Always include the system message if present
                    system_messages = [m for m in messages if m.get('role') == 'system']
                    other_messages = [m for m in messages if m.get('role') != 'system']
                    
                    # Keep system messages and last 9 other messages
                    truncated_messages = system_messages + other_messages[-9:]
                    data['messages'] = truncated_messages
                    logger.info(f"Truncated to {len(truncated_messages)} messages")
            
            # Get the original model name for later use
            original_model = data.get('model', 'default-model')
            
            # Format the request data for the specific provider
            request_data = format_request_for_provider(data)
        else:
            try:
                data = json.loads(request.data.decode('utf-8'))
                logger.info(f"Non-JSON request parsed for model: {data.get('model', 'unknown')}")
                original_model = data.get('model', 'default-model')
                request_data = format_request_for_provider(data)
            except:
                logger.error("Failed to parse request data")
                original_model = "default-model"
                request_data = {}
        
        # Check cache for this exact request
        cache_key = None
        if request.is_json:
            try:
                cache_key = json.dumps(data, sort_keys=True)
                if cache_key in request_cache:
                    logger.info("Using cached response for duplicate request")
                    return request_cache[cache_key]
            except Exception as e:
                logger.error(f"Error checking cache: {str(e)}")
        
        # Get provider-specific information
        base_url, endpoint = get_provider_url_and_endpoint()
        auth_headers = get_provider_auth_headers()
        
        # Modify request for streaming if supported
        if AI_PROVIDER in ["groq", "grok", "anthropic", "ollama"]:
            # These providers support streaming
            request_data['stream'] = True
        else:
            # For providers that don't support streaming, we'll use non-streaming
            request_data['stream'] = False
        
        logger.info(f"Sending request to {AI_PROVIDER.upper()} API")
        log_raw_data(f"{AI_PROVIDER.upper()} REQUEST", request_data)
        
        # Google API might need special handling for the API key
        full_url = f"{base_url}{endpoint}"
        if AI_PROVIDER == "google":
            full_url += f"?key={GOOGLE_API_KEY}"
        
        def generate():
            try:
                # Create a list to collect streaming chunks for logging
                collected_chunks = []
                
                # Track if we're in a code block to prevent premature closing
                in_code_block = False
                code_block_count = 0
                last_chunk_time = time.time()
                
                # For non-streaming providers, handle differently
                if AI_PROVIDER not in ["groq", "grok", "anthropic", "ollama"]:
                    # Non-streaming approach
                    response = requests.post(
                        full_url,
                        json=request_data,
                        headers=auth_headers,
                        timeout=API_TIMEOUT
                    )
                    
                    if response.status_code != 200:
                        error_msg = response.text[:200] if hasattr(response, 'text') else "Unknown error"
                        logger.error(f"API error: {response.status_code} - {error_msg}")
                        error_response = {
                            "id": f"chatcmpl-{uuid.uuid4()}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": original_model,
                            "choices": [{
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": f"**Error: {error_msg}**\n\nPlease try a different approach or ask the user for guidance."
                                },
                                "finish_reason": "stop"
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        }
                        
                        log_raw_data("ERROR RESPONSE", error_response)
                        yield f"data: {json.dumps(error_response)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    
                    # Parse the response
                    provider_response = response.json()
                    log_raw_data(f"{AI_PROVIDER.upper()} RESPONSE", provider_response)
                    
                    # Format the response to match OpenAI format
                    openai_response = format_response_for_openai(provider_response, original_model)
                    
                    # Return the response as a single event
                    yield f"data: {json.dumps(openai_response)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                
                # For streaming providers
                with requests.post(
                    full_url,
                    json=request_data,
                    headers=auth_headers,
                    stream=True,
                    timeout=API_TIMEOUT
                ) as provider_response:
                    
                    # Check for error status
                    if provider_response.status_code != 200:
                        error_msg = provider_response.text[:200] if hasattr(provider_response, 'text') else "Unknown error"
                        logger.error(f"API error: {provider_response.status_code} - {error_msg}")
                        error_response = {
                            "id": f"chatcmpl-{uuid.uuid4()}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": original_model,
                            "choices": [{
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": f"**Error: {error_msg}**\n\nPlease try a different approach or ask the user for guidance."
                                },
                                "finish_reason": "stop"
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        }
                        
                        log_raw_data("ERROR RESPONSE", error_response)
                        yield f"data: {json.dumps(error_response)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    # Process the streaming response
                    for line in provider_response.iter_lines():
                        if line:
                            line = line.decode('utf-8')
                            last_chunk_time = time.time()
                            
                            # Collect the chunk for logging
                            collected_chunks.append(line)
                            
                            # Check if we're entering or exiting a code block
                            if line.startswith('data: ') and '"content":"```' in line:
                                in_code_block = True
                                code_block_count += 1
                                logger.info(f"Entering code block #{code_block_count}")
                            elif line.startswith('data: ') and '"content":"```' in line and in_code_block:
                                in_code_block = False
                                logger.info(f"Exiting code block #{code_block_count}")
                            
                            if line.startswith('data: '):
                                # Pass through the streaming data
                                # For Anthropic, we'd need to transform their SSE format to OpenAI format
                                if AI_PROVIDER == "anthropic":
                                    try:
                                        # Handle Anthropic's SSE format
                                        if line.startswith('data: ') and line[6:].strip():
                                            anthropic_data = json.loads(line[6:])
                                            
                                            # Check for completion event
                                            if anthropic_data.get('type') == 'content_block_delta':
                                                delta_text = anthropic_data.get('delta', {}).get('text', '')
                                                
                                                # Create OpenAI-style chunk
                                                openai_chunk = {
                                                    "id": f"chatcmpl-{uuid.uuid4()}",
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": original_model,
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {"content": delta_text},
                                                        "finish_reason": None
                                                    }]
                                                }
                                                
                                                yield f"data: {json.dumps(openai_chunk)}\n\n"
                                    except json.JSONDecodeError:
                                        # If it's not JSON, just pass it through
                                        yield f"{line}\n\n"
                                elif AI_PROVIDER == "ollama":
                                    try:
                                        # Handle Ollama's SSE format
                                        if line.startswith('data: ') and line[6:].strip():
                                            ollama_data = json.loads(line[6:])
                                            
                                            if 'message' in ollama_data and 'content' in ollama_data['message']:
                                                content = ollama_data['message']['content']
                                                
                                                # Create OpenAI-style chunk
                                                openai_chunk = {
                                                    "id": f"chatcmpl-{uuid.uuid4()}",
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": original_model,
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {"content": content},
                                                        "finish_reason": None
                                                    }]
                                                }
                                                
                                                yield f"data: {json.dumps(openai_chunk)}\n\n"
                                    except json.JSONDecodeError:
                                        # If it's not JSON, just pass it through
                                        yield f"{line}\n\n"
                                else:
                                    # For Groq, Grok, and custom, pass through directly
                                    yield f"{line}\n\n"
                            elif line.strip() == 'data: [DONE]':
                                yield "data: [DONE]\n\n"
                                return  # Ensure we exit the generator after [DONE]
                    
                    # Log all collected chunks at once
                    if collected_chunks:
                        log_raw_data("STREAMING RESPONSE (COMPLETE)", 
                                    collect_streaming_chunks(collected_chunks))
                    
                    # If we were in a code block, make sure we send a proper closing
                    if in_code_block:
                        logger.info("Detected unclosed code block, sending closing marker")
                        # Send a dummy chunk to keep the connection alive
                        dummy_chunk = {
                            "id": f"chatcmpl-{uuid.uuid4()}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": original_model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": ""},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(dummy_chunk)}\n\n"
                    
                    # Always send a final [DONE] marker
                    yield "data: [DONE]\n\n"
                    
                    # Wait a moment before closing to ensure all data is processed
                    time.sleep(0.5)

            except requests.exceptions.Timeout:
                logger.error("API timeout")
                error_response = {
                    "error": {
                        "message": "Request timeout",
                        "type": "timeout_error",
                        "code": "timeout"
                    }
                }
                log_raw_data("TIMEOUT ERROR", error_response)
                yield f"data: {json.dumps(error_response)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.error(f"Error during streaming: {str(e)}")
                error_response = {
                    "error": {
                        "message": str(e),
                        "type": "server_error",
                        "code": "stream_error"
                    }
                }
                log_raw_data("STREAMING ERROR", {"error": str(e), "traceback": traceback.format_exc()})
                yield f"data: {json.dumps(error_response)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                # Clear the cache after processing the request
                if cache_key in request_cache:
                    del request_cache[cache_key]
                    logger.info("Cache cleared for request")
                
                # Remove this request from the streaming tracker
                if request_hash in streaming_tracker:
                    del streaming_tracker[request_hash]
                    logger.info(f"Removed request from streaming tracker (hash: {request_hash})")

        # Return a streaming response with proper headers
        response = app.response_class(
            generate(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'access-control-expose-headers': 'X-Request-ID',
                'x-request-id': request_id
            }
        )
        
        logger.info(f"Started streaming response (request ID: {request_id})")
        return response
            
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        logger.error(traceback.format_exc())
        
        # Create a properly structured error response
        error_response_data = {
            "error": {
                "message": str(e),
                "type": "server_error",
                "param": None,
                "code": "no_completion"
            }
        }
        
        error_response = make_response(jsonify(error_response_data))
        error_response.status_code = 500
        error_response.headers.add('Content-Type', 'application/json')
        
        return error_response

# ============================================================================
# ROUTE DEFINITIONS
# ============================================================================

# Route for standard OpenAI endpoint
@app.route(OPENAI_CHAT_ENDPOINT, methods=['POST', 'OPTIONS'])
def openai_chat_completions():
    """Handle requests to the standard OpenAI chat completions endpoint"""
    logger.info(f"Request to standard OpenAI endpoint")
    return process_chat_request()

# Route for Cursor's custom endpoint
@app.route(CURSOR_CHAT_ENDPOINT, methods=['POST', 'OPTIONS'])
def cursor_chat_completions():
    """Handle requests to Cursor's custom chat completions endpoint"""
    logger.info(f"Request to Cursor endpoint")
    return process_chat_request()

# Catch-all route for any other chat completions endpoint
@app.route('/<path:path>/chat/completions', methods=['POST', 'OPTIONS'])
def any_chat_completions(path):
    """Handle requests to any other chat completions endpoint"""
    logger.info(f"Request to custom path: /{path}/chat/completions")
    return process_chat_request()

# Add a route for OpenAI's models endpoint
@app.route('/v1/models', methods=['GET', 'OPTIONS'])
def list_models():
    """Return a list of available models that match the OpenAI models format"""
    logger.info("Request to models endpoint")
    
    # Create a list of model objects based on the model mappings
    models = []
    for openai_model in MODEL_MAPPINGS[AI_PROVIDER].keys():
        models.append({
            "id": openai_model,
            "object": "model",
            "created": 1700000000,
            "owned_by": "openai"
        })
    
    # Create response with OpenAI-specific headers
    response = make_response(jsonify({"data": models, "object": "list"}))
    
    # Add OpenAI specific headers
    response.headers.add('access-control-expose-headers', 'X-Request-ID')
    response.headers.add('openai-organization', 'user-custom-organization')
    response.headers.add('openai-processing-ms', '10')
    response.headers.add('openai-version', '2020-10-01')
    response.headers.add('strict-transport-security', 'max-age=15724800; includeSubDomains')
    response.headers.add('x-request-id', str(uuid.uuid4()))
    
    # Set correct Content-Type header
    response.headers.set('Content-Type', 'application/json')
    
    return response

# Add health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    """Return health status of the proxy server"""
    logger.info("Health check request")
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "uptime": time.time() - start_time,
        "provider": AI_PROVIDER,
        "api_key_set": bool(get_provider_api_key())
    })

# Add a simple direct endpoint for non-streaming single-message exchange
@app.route('/direct', methods=['POST', 'OPTIONS'])
def direct_completion():
    """Simple endpoint that takes a single message and returns a response"""
    logger.info("Request to direct endpoint")
    
    if request.method == 'OPTIONS':
        return handle_options('direct')
    
    try:
        # Get the request data
        if request.is_json:
            data = request.json
            message = data.get('message', '')
            model = data.get('model', MODEL_MAPPINGS[AI_PROVIDER]["default"])
            logger.info(f"Direct request for model: {model}")
        else:
            try:
                data = json.loads(request.data.decode('utf-8'))
                message = data.get('message', '')
                model = data.get('model', MODEL_MAPPINGS[AI_PROVIDER]["default"])
            except:
                logger.error("Failed to parse direct request data")
                return jsonify({"error": "Invalid request format"}), 400
        
        # Create a simple request to the provider
        provider_model = MODEL_MAPPINGS[AI_PROVIDER].get(model, MODEL_MAPPINGS[AI_PROVIDER]["default"])
        provider_request = {
            "model": provider_model,
            "messages": [
                {"role": "user", "content": message}
            ],
            "stream": False  # No streaming for direct endpoint
        }
        
        # Forward the request to the provider
        base_url, endpoint = get_provider_url_and_endpoint()
        auth_headers = get_provider_auth_headers()
        
        full_url = f"{base_url}{endpoint}"
        if AI_PROVIDER == "google":
            full_url += f"?key={GOOGLE_API_KEY}"
        
        logger.info(f"Sending direct request to {AI_PROVIDER}")
        log_raw_data("DIRECT REQUEST", provider_request)
        
        response = requests.post(
            full_url,
            json=provider_request,
            headers=auth_headers,
            timeout=API_TIMEOUT
        )
        
        if response.status_code != 200:
            logger.error(f"API error: {response.status_code} - {response.text[:200]}")
            log_raw_data("DIRECT ERROR RESPONSE", response.text)
            return jsonify({
                "error": f"API error: {response.status_code}",
                "message": "Failed to get response from provider"
            }), response.status_code
        
        # Parse the response
        log_raw_data("DIRECT RAW RESPONSE", response.text)
        provider_response = response.json()
        log_raw_data("DIRECT PARSED RESPONSE", provider_response)
        
        # Format the provider response to OpenAI format
        formatted_response = format_response_for_openai(provider_response, model)
        
        # Extract just the content from the response
        if "choices" in formatted_response and len(formatted_response["choices"]) > 0:
            content = formatted_response["choices"][0]["message"]["content"]
            result = {"response": content}
            log_raw_data("DIRECT FINAL RESPONSE", result)
            return jsonify(result)
        else:
            return jsonify({"error": "No response content found"}), 500
            
    except Exception as e:
        logger.error(f"Error processing direct request: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

# Add an agent mode endpoint that includes specific instructions
@app.route('/agent', methods=['POST', 'OPTIONS'])
def agent_mode():
    """Handle requests with agent mode instructions included"""
    logger.info("Request to agent mode endpoint")
    
    if request.method == 'OPTIONS':
        return handle_options('agent')
    
    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400
            
        # Get the request data
        data = request.json.copy()
        
        # Add agent instructions to system message
        if 'messages' in data:
            messages = data['messages']
            
            # Check if there's a system message
            has_system = False
            for msg in messages:
                if msg.get('role') == 'system':
                    has_system = True
                    # Append agent instructions to existing system message if not already there
                    if AGENT_INSTRUCTIONS not in msg['content']:
                        msg['content'] += f"\n\n{AGENT_INSTRUCTIONS}"
                    break
            
            if not has_system:
                # Insert a system message with the agent instructions at the beginning
                messages.insert(0, {
                    "role": "system",
                    "content": AGENT_INSTRUCTIONS
                })
        
        # Continue with the standard request processing
        return process_chat_request()
            
    except Exception as e:
        logger.error(f"Error processing agent mode request: {str(e)}")
        logger.error(traceback.format_exc())
        
        # Create a properly structured error response
        error_response_data = {
            "error": {
                "message": str(e),
                "type": "server_error",
                "param": None,
                "code": "no_completion"
            }
        }
        
        return jsonify(error_response_data), 500

@app.route('/', methods=['GET'])
def home():
    """Render a home page with basic information about the proxy"""
    logger.info("Home page request")
    return """
    <html>
    <head>
        <title>Multi-Provider AI Proxy</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            pre { background-color: #f5f5f5; padding: 10px; border-radius: 5px; overflow-x: auto; }
            .endpoint { background-color: #e0f7fa; padding: 10px; margin: 10px 0; border-radius: 5px; }
            h1, h2 { color: #333; }
            .provider { font-weight: bold; color: #0277bd; }
        </style>
    </head>
    <body>
        <h1>Multi-Provider AI Proxy Server</h1>
        <p>This server proxies requests to various AI providers while maintaining OpenAI API compatibility.</p>
        <p>Currently using provider: <span class="provider">""" + AI_PROVIDER + """</span></p>
        
        <h2>Available Endpoints</h2>
        <div class="endpoint">
            <h3>/v1/chat/completions (Standard OpenAI endpoint)</h3>
            <p>Use this endpoint for standard OpenAI API compatibility</p>
        </div>
        <div class="endpoint">
            <h3>/chat/completions (Cursor endpoint)</h3>
            <p>Use this endpoint for Cursor compatibility</p>
        </div>
        <div class="endpoint">
            <h3>/direct (Direct endpoint)</h3>
            <p>Simple endpoint that takes a single message and returns a response</p>
        </div>
        <div class="endpoint">
            <h3>/agent (Agent mode endpoint)</h3>
            <p>Endpoint with agent mode instructions included in system prompt</p>
        </div>
        
        <h2>Test the API</h2>
        <p>You can test the API with the following curl command:</p>
        <pre>
curl -X POST \\
  http://localhost:5000/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer fake-api-key" \\
  -d '{
    "model": "gpt-4o",
    "messages": [
      {"role": "system", "content": "You are a test assistant."},
      {"role": "user", "content": "Testing. Just say hi and nothing else."}
    ]
  }'
        </pre>
        
        <h2>Debug Information</h2>
        <p>For debug information, visit <a href="/debug">/debug</a></p>
        <p>For health check, visit <a href="/health">/health</a></p>
    </body>
    </html>
    """

def start_ngrok(port):
    """Start ngrok and return the public URL - useful for development and demos"""
    try:
        # Check if ngrok is installed
        try:
            subprocess.run(["ngrok", "--version"], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("ngrok is not installed or not in PATH. Please install ngrok first.")
            print("ngrok is not installed or not in PATH. Please install ngrok first.")
            print("Visit https://ngrok.com/download to download and install ngrok")
            return None
            
        # Start ngrok with recommended settings for Cursor
        logger.info(f"Starting ngrok on port {port}...")
        ngrok_process = subprocess.Popen(
            ["ngrok", "http", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        logger.info(f"Started ngrok process (PID: {ngrok_process.pid})")
        
        # Wait for ngrok to start
        logger.info("Waiting for ngrok to initialize...")
        time.sleep(3)
        
        # Get the public URL from ngrok API
        try:
            logger.info("Requesting tunnel information from ngrok API...")
            response = requests.get("http://localhost:4040/api/tunnels")
            tunnels = response.json()["tunnels"]
            if tunnels:
                # Using https tunnel is recommended for Cursor
                https_tunnels = [t for t in tunnels if t["public_url"].startswith("https")]
                if https_tunnels:
                    public_url = https_tunnels[0]["public_url"]
                else:
                    public_url = tunnels[0]["public_url"]
                
                logger.info(f"ngrok public URL: {public_url}")
                
                print(f"\n{'='*60}")
                print(f"NGROK PUBLIC URL: {public_url}")
                print(f"NGROK INSPECTOR: http://localhost:4040")
                print(f"Use this URL in Cursor as your OpenAI API base URL")
                print(f"{'='*60}\n")
                
                # Print instructions for Cursor
                print("\nTo configure Cursor:")
                print(f"1. Set the OpenAI API base URL to: {public_url}")
                print("2. Use any OpenAI model name that Cursor supports")
                print("3. Set any API key (it won't be checked)")
                print("4. Check the ngrok inspector at http://localhost:4040 to debug traffic")
                
                return public_url
            else:
                logger.error("No ngrok tunnels found")
                print("No ngrok tunnels found. Please check ngrok configuration.")
                return None
        except Exception as e:
            logger.error(f"Error getting ngrok URL: {str(e)}")
            print(f"Error getting ngrok URL: {str(e)}")
            return None
    except Exception as e:
        logger.error(f"Error starting ngrok: {str(e)}")
        print(f"Error starting ngrok: {str(e)}")
        return None

# Store app start time for uptime tracking
start_time = time.time()

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    use_ngrok = os.environ.get("USE_NGROK", "0") == "1"
    
    logger.info(f"Starting Multi-Provider AI Proxy server on port {port}")
    logger.info(f"Using provider: {AI_PROVIDER}")
    
    # Start ngrok if requested
    if use_ngrok:
        public_url = start_ngrok(port)
    
    # Start the Flask server
    print(f"Starting Multi-Provider AI Proxy server on port {port}")
    print(f"Using AI provider: {AI_PROVIDER}")
    print(f"Debug info available at: http://localhost:{port}/debug")
    print(f"Health check available at: http://localhost:{port}/health")
    
    try:
        # Use Waitress WSGI server for production-ready serving
        serve(app, host="0.0.0.0", port=port)
    except Exception as e:
        logger.critical(f"Server failed to start: {str(e)}")
        print(f"Server failed to start: {str(e)}")
        sys.exit(1)
