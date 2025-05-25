#!/usr/bin/env python3

import os
import shutil
import subprocess
import datetime
import sys
import collections
import json
import openai
from dotenv import load_dotenv # For securely loading API key

# --- Configuration ---
# Directories to scan for overall size analysis.
SCAN_DIRS_FOR_SIZE = [
    os.path.expanduser('~'),  # Your home directory
    '/Library/Caches',
    '/private/var/folders', # Common temporary files location
    '/tmp',
]

# Directories to specifically scan for large/old/cache/temp files.
SCAN_DIRS_FOR_SUGGESTIONS = [
    os.path.expanduser('~'),
    '/Library/Caches',
    '/private/var/folders',
    '/tmp',
]

# Files larger than this (in MB) will be flagged as "large".
MIN_LARGE_FILE_SIZE_MB = 100

# Files older than this many days will be flagged as "old".
OLD_FILE_DAYS = 90

# Keywords/patterns to identify potential cache, temp, or log files.
CACHE_PATTERNS = ['cache', '.cache', 'chromium', 'vscode', 'npm', 'yarn', 'brew', 'library/developer/xcode/deriveddata']
TEMP_PATTERNS = ['temp', '.tmp', 'temporary', 'downloads', 'trash', '.Trash']
LOG_PATTERNS = ['log', '.log', 'logs']

# Number of top directories to display by default (can be overridden by agent)
DEFAULT_TOP_N_DIRS = 15

# --- Global Data Storage (Populated by initial scan) ---
_disk_summary_data = {}
_directory_sizes_data = [] # List of (path, size_bytes) tuples
_suggested_files_data = collections.defaultdict(list) # Dict of {suggestion_type: [(filepath, size)]}

# --- Helper Functions (from previous script, adapted to return data) ---

def convert_bytes_to_human_readable(num_bytes):
    """Converts a number of bytes into a human-readable string (e.g., 10GB, 500MB)."""
    if num_bytes is None:
        return "N/A"
    num_bytes = float(num_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"

def convert_human_readable_to_bytes(size_str):
    """Converts a human-readable size string (e.g., '1.2G', '500M') to bytes."""
    size_str = size_str.strip().upper()
    if not size_str:
        return 0

    # Extract number part
    num_str = ""
    unit_str = ""
    for char in size_str:
        if char.isdigit() or char == '.':
            num_str += char
        else:
            unit_str += char

    try:
        num = float(num_str)
    except ValueError:
        return 0 # Cannot parse number

    if 'K' in unit_str:
        return num * 1024
    elif 'M' in unit_str:
        return num * 1024**2
    elif 'G' in unit_str:
        return num * 1024**3
    elif 'T' in unit_str:
        return num * 1024**4
    elif 'P' in unit_str:
        return num * 1024**5
    elif 'B' in unit_str: # Explicit bytes or no unit
        return num
    return num # Default to bytes if no unit found

def is_old_file(filepath, days_threshold):
    """Checks if a file is older than a given number of days."""
    try:
        mtime = os.path.getmtime(filepath)
        file_age_seconds = datetime.datetime.now().timestamp() - mtime
        file_age_days = file_age_seconds / (24 * 3600)
        return file_age_days > days_threshold
    except (OSError, FileNotFoundError):
        return False

def classify_file(filepath):
    """Classifies a file based on its path for suggestion purposes."""
    filepath_lower = filepath.lower()
    if any(p in filepath_lower for p in CACHE_PATTERNS):
        return "Cache"
    if any(p in filepath_lower for p in TEMP_PATTERNS):
        return "Temporary"
    if any(p in filepath_lower for p in LOG_PATTERNS):
        return "Log"
    return "Other"

# --- Data Collection Functions (Populate global data) ---

def run_initial_scan():
    """Performs the initial disk scan and populates global data."""
    print("--- Initial Disk Scan ---")
    print("This may take a few moments depending on your disk size and speed.")
    print("Scanning...")

    # 1. Get Disk Summary
    try:
        total, used, free = shutil.disk_usage("/")
        _disk_summary_data.update({
            "total": total,
            "used": used,
            "free": free,
            "usage_percentage": used / total if total > 0 else 0
        })
        print(f"  Disk Summary Collected: Used {convert_bytes_to_human_readable(used)}")
    except Exception as e:
        print(f"  Error getting disk summary: {e}")
        _disk_summary_data.clear()

    # 2. Get Top Directory Sizes
    print(f"  Scanning top-level directories: {', '.join(SCAN_DIRS_FOR_SIZE)}")
    dir_sizes = {}
    for path in SCAN_DIRS_FOR_SIZE:
        if not os.path.exists(path):
            print(f"    Warning: Directory not found: {path}. Skipping.")
            continue
        try:
            process = subprocess.run(
                ['du', '-sh', path],
                capture_output=True,
                text=True,
                check=True,
                encoding='utf-8' # Ensure correct encoding for file paths
            )
            output = process.stdout.strip().split('\t')
            if len(output) == 2:
                size_str, dir_path = output
                dir_sizes[dir_path] = convert_human_readable_to_bytes(size_str)
            else:
                print(f"    Warning: Could not parse du output for {path}: {output}")
        except subprocess.CalledProcessError as e:
            print(f"    Error running 'du' for {path}: {e.stderr.strip()}")
        except Exception as e:
            print(f"    An unexpected error occurred for {path}: {e}")

    _directory_sizes_data.extend(sorted(dir_sizes.items(), key=lambda item: item[1], reverse=True))
    print(f"  Top Directory Sizes Collected ({len(_directory_sizes_data)} entries).")

    # 3. Find Large/Old/Cache/Temp Files for Suggestions
    print(f"  Scanning for potential cleanup suggestions in: {', '.join(SCAN_DIRS_FOR_SUGGESTIONS)}")
    min_size_bytes = MIN_LARGE_FILE_SIZE_MB * 1024 * 1024

    for directory in SCAN_DIRS_FOR_SUGGESTIONS:
        if not os.path.exists(directory):
            print(f"    Warning: Suggestion scan directory not found: {directory}. Skipping.")
            continue
        # print(f"    Scanning {directory}...")
        for root, dirs, files in os.walk(directory, followlinks=False): # Don't follow symlinks to avoid loops and double counting
            # Prune directories that are likely permission-denied or irrelevant for this scan
            dirs[:] = [d for d in dirs if not d.startswith(('.', '$')) and d != 'tmp'] # Skip hidden system folders, Windows tmp, etc.
            if 'Library/Containers' in root and os.path.expanduser('~') in root:
                # Many app sandboxes are in here, often restricted or not relevant for manual cleanup
                # unless specifically targeting an app's data. Prune deep dives.
                if 'Containers' in dirs: dirs.remove('Containers')
            if '.Trash' in dirs: dirs.remove('.Trash') # handled by specific patterns


            for name in files:
                filepath = os.path.join(root, name)
                try:
                    file_size = os.path.getsize(filepath)

                    is_large = file_size >= min_size_bytes
                    is_old = is_old_file(filepath, OLD_FILE_DAYS)
                    file_type = classify_file(filepath)

                    if is_large or is_old or file_type != "Other":
                        suggestion_type = []
                        if is_large:
                            suggestion_type.append("Large")
                        if is_old:
                            suggestion_type.append("Old")
                        if file_type != "Other":
                            suggestion_type.append(file_type)

                        if suggestion_type:
                            _suggested_files_data[tuple(sorted(suggestion_type))].append({"path": filepath, "size": file_size})

                except PermissionError:
                    pass # Silently skip permission errors for individual files
                except FileNotFoundError:
                    pass # File might have been deleted between os.walk and os.path.getsize
                except OSError as e: # Catch other OS-related errors like invalid file names
                    # print(f"    OS Error processing {filepath}: {e}")
                    pass
                except Exception as e:
                    # print(f"    Error processing {filepath}: {e}")
                    pass

    print(f"  Cleanup Suggestions Collected ({sum(len(v) for v in _suggested_files_data.values())} potential files).")
    print("Initial scan complete. You can now ask questions.")

# --- LLM Tools (Functions the AI agent can call) ---

def get_overall_disk_info():
    """
    Returns the overall disk usage information for the root partition,
    including total, used, free space, and usage percentage.
    """
    if not _disk_summary_data:
        return "Disk summary data not available."
    return json.dumps({
        "total": convert_bytes_to_human_readable(_disk_summary_data.get("total")),
        "used": convert_bytes_to_human_readable(_disk_summary_data.get("used")),
        "free": convert_bytes_to_human_readable(_disk_summary_data.get("free")),
        "usage_percentage": f"{_disk_summary_data.get('usage_percentage', 0):.2%}"
    })

def get_top_n_directories(n: int = DEFAULT_TOP_N_DIRS):
    """
    Get a list of the largest directories and their sizes.
    Args:
        n (int): The number of top directories to return. Defaults to 15.
    Returns:
        A JSON string of a list of dictionaries, each with 'path' and 'size'.
    """
    if not _directory_sizes_data:
        return "No directory size data available. Please ensure the initial scan completed successfully."
    top_dirs = [{"path": path, "size": convert_bytes_to_human_readable(size)}
                for path, size in _directory_sizes_data[:n]]
    return json.dumps(top_dirs)

def get_suggested_files(suggestion_type: str = None, limit: int = 10):
    """
    Get a list of suggested files for review, optionally filtered by type.
    Types can be 'Large', 'Old', 'Cache', 'Temporary', 'Log'.
    You can combine types (e.g., 'Large, Old', 'Cache, Temporary').
    If no type is specified, returns a summary of all types.

    Args:
        suggestion_type (str, optional): A comma-separated string of types to filter by.
                                         Example: "Large, Cache". Case-insensitive.
                                         Defaults to None (returns summary).
        limit (int): The maximum number of files to return per category. Defaults to 10.
    Returns:
        A JSON string containing the suggestions.
    """
    if not _suggested_files_data:
        return "No specific cleanup suggestions found based on current criteria."

    results = {}
    requested_types = []
    if suggestion_type:
        requested_types = [s.strip().capitalize() for s in suggestion_type.split(',')]

    all_sorted_types = sorted(list(_suggested_files_data.keys()), key=lambda x: len(x), reverse=True)

    for s_type_tuple in all_sorted_types:
        s_type_name = " ".join(s_type_tuple)
        if requested_types and not any(rt in s_type_name for rt in requested_types):
            continue

        items = sorted(_suggested_files_data[s_type_tuple], key=lambda x: x['size'], reverse=True)
        formatted_items = []
        for item in items[:limit]:
            formatted_items.append({
                "path": item['path'],
                "size": convert_bytes_to_human_readable(item['size'])
            })
        results[s_type_name] = formatted_items

    if not results and requested_types:
        return f"No suggestions found for types: {suggestion_type}. Available types: {', '.join(set(t for types in _suggested_files_data.keys() for t in types))}"
    elif not results:
         return "No specific cleanup suggestions found based on current criteria."

    # If no specific type was requested, provide a summary
    if not requested_types:
        summary = {
            "summary": {
                "total_suggestions": sum(len(v) for v in _suggested_files_data.values()),
                "categories_found": {
                    " ".join(k): len(v) for k, v in _suggested_files_data.items()
                }
            },
            "details_hint": "You can ask for specific categories like 'Large', 'Old', 'Cache', 'Temporary', 'Log', or combinations like 'Large, Old'."
        }
        return json.dumps(summary, indent=2)

    return json.dumps(results, indent=2)


def search_paths(query: str):
    """
    Search for directories or files containing a specific keyword in their path.
    Args:
        query (str): The keyword to search for in paths.
    Returns:
        A JSON string of a list of paths found.
    """
    query_lower = query.lower()
    found_paths = set()

    for path, _ in _directory_sizes_data:
        if query_lower in path.lower():
            found_paths.add(path)

    for s_type_tuple in _suggested_files_data:
        for item in _suggested_files_data[s_type_tuple]:
            if query_lower in item['path'].lower():
                found_paths.add(item['path'])

    if not found_paths:
        return f"No paths found containing '{query}' in scan results."
    return json.dumps(list(found_paths)[:20]) # Limit to 20 to avoid overwhelming

# --- LLM Tool Definitions for OpenAI API ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_overall_disk_info",
            "description": "Returns the overall disk usage information for the root partition, including total, used, free space, and usage percentage.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_n_directories",
            "description": "Get a list of the largest directories and their sizes. Use this to find out where most data is stored.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "The number of top directories to return. Defaults to 15 if not specified."
                    }
                },
                "required": [] # n is optional
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_suggested_files",
            "description": "Get a list of suggested files for review, filtered by type (e.g., 'Large', 'Old', 'Cache', 'Temporary', 'Log'). This function identifies files that might be good candidates for cleanup. If no type is specified, it provides a summary of all suggestion categories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "suggestion_type": {
                        "type": "string",
                        "description": "A comma-separated string of types to filter by. Example: 'Large, Cache'. Case-insensitive. Leave empty for a general summary."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "The maximum number of files to return per category. Defaults to 10."
                    }
                },
                "required": [] # suggestion_type and limit are optional
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_paths",
            "description": "Search for directories or files containing a specific keyword in their path. Useful for finding data related to a specific application or type (e.g., 'Xcode', 'photos', 'downloads').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The keyword to search for in paths (case-insensitive)."
                    }
                },
                "required": ["query"]
            }
        }
    }
]

# Map tool names to actual Python functions
AVAILABLE_FUNCTIONS = {
    "get_overall_disk_info": get_overall_disk_info,
    "get_top_n_directories": get_top_n_directories,
    "get_suggested_files": get_suggested_files,
    "search_paths": search_paths,
}

# --- Main Agent Logic ---

def run_conversation():
    # Load API key from environment variable or .env file
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        print("\n--- OpenAI API Key Setup ---")
        print("You need to set your OpenAI API key to use the AI assistant.")
        print("1. Visit https://platform.openai.com/account/api-keys to get your key.")
        print("2. You can set it as an environment variable (OPENAI_API_KEY) or")
        print("   enter it here directly. For security, environment variable is preferred.")
        api_key = input("Enter your OpenAI API Key (or press Enter to try environment variable): ").strip()
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Error: OpenAI API key not found. Please set it as an environment variable (OPENAI_API_KEY)")
            print("or provide it when prompted. Exiting.")
            sys.exit(1)

    openai.api_key = api_key

    client = openai.OpenAI(api_key=api_key)

    messages = [
        {"role": "system", "content": (
            "You are a helpful and informative MacBook Disk Usage Assistant. "
            "Your goal is to answer questions about disk usage and file sizes based on the provided scan data. "
            "You have access to specific tools to retrieve this data. "
            "**Crucially, you cannot delete, modify, or create any files or directories.** "
            "Only provide information and suggestions for manual review. "
            "When presenting data, use clear and structured formats, like bullet points or tables. "
            "Always state that you cannot delete or modify files when making suggestions."
        )}
    ]

    print("\n--- MacBook Disk Usage AI Agent ---")
    print("Welcome! I've completed the initial disk scan.")
    print("You can now ask me questions about your disk usage.")
    print("Examples:")
    print("  - How much disk space is used?")
    print("  - What are the largest directories?")
    print("  - Show me files that are large and old.")
    print("  - Are there any temporary files I can review?")
    print("  - Where is Xcode's derived data?")
    print("Type 'q' or 'exit' to quit.")

    while True:
        user_query = input("\nYour question: ").strip()
        if user_query.lower() in ['q', 'quit', 'exit']:
            print("Exiting Disk Usage Agent. Goodbye!")
            break

        messages.append({"role": "user", "content": user_query})

        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo-0125", # You can try "gpt-4" or other models if available
                messages=messages,
                tools=TOOLS,
                tool_choice="auto", # Let the model decide whether to call a tool
            )
            response_message = response.choices[0].message
            tool_calls = response_message.tool_calls

            # Step 2: check if the model wanted to call a tool
            if tool_calls:
                # print(f"DEBUG: Model requested tool calls: {tool_calls}") # For debugging
                messages.append(response_message)  # extend conversation with assistant's reply
                
                # Step 3: call the tool
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_to_call = AVAILABLE_FUNCTIONS.get(function_name)
                    if function_to_call:
                        try:
                            function_args = json.loads(tool_call.function.arguments)
                            function_response = function_to_call(**function_args)
                            messages.append(
                                {
                                    "tool_call_id": tool_call.id,
                                    "role": "tool",
                                    "name": function_name,
                                    "content": function_response,
                                }
                            )
                        except json.JSONDecodeError:
                            error_message = f"Error: Failed to parse arguments for {function_name}. Args: {tool_call.function.arguments}"
                            print(f"Agent Error: {error_message}")
                            messages.append({"role": "tool", "name": function_name, "content": error_message})
                        except Exception as e:
                            error_message = f"Error executing tool '{function_name}': {e}"
                            print(f"Agent Error: {error_message}")
                            messages.append({"role": "tool", "name": function_name, "content": error_message})
                    else:
                        error_message = f"Error: Tool '{function_name}' not found."
                        print(f"Agent Error: {error_message}")
                        messages.append({"role": "tool", "name": function_name, "content": error_message})

                # Step 4: send the info back to the model
                second_response = client.chat.completions.create(
                    model="gpt-3.5-turbo-0125",
                    messages=messages,
                )  # get a new response from the model that can summarize the tool's output
                print(second_response.choices[0].message.content)
                messages.append(second_response.choices[0].message) # Add agent's response to history

            else:
                print(response_message.content)
                messages.append(response_message) # Add agent's response to history

        except openai.AuthenticationError:
            print("\nError: Invalid OpenAI API key. Please check your key.")
            print("Exiting.")
            sys.exit(1)
        except openai.APITimeoutError:
            print("\nError: OpenAI API request timed out. Please try again.")
        except openai.APIConnectionError as e:
            print(f"\nError: Could not connect to OpenAI API: {e}")
        except openai.RateLimitError:
            print("\nError: OpenAI API rate limit exceeded. Please wait a moment and try again.")
        except Exception as e:
            print(f"\nAn unexpected error occurred with the AI agent: {e}")
            print("Please try again or restart the script.")


# --- Main Execution ---

if __name__ == "__main__":
    print("Starting MacBook Disk Usage AI Agent...")
    print("---------------------------------------")
    print("NOTE: This script ONLY analyzes and suggests. It DOES NOT delete or modify any files.")
    print("Permissions: For full system scans, you might need 'sudo'. However, it's safer to run")
    print("             without 'sudo' to avoid scanning system-critical directories, which are")
    print("             usually not the source of user-related disk space issues.")
    print("Data freshness: All data is based on the initial scan. Restart the script for fresh data.")


    run_initial_scan()
    run_conversation()