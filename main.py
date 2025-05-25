#!/usr/bin/env python3

import os
import shutil
import subprocess
import datetime
import sys
import collections

# --- Configuration ---
# Directories to scan for overall size analysis.
# These should generally be user-accessible.
# Adding /Library directly might require sudo for full access, which is not recommended
# for a purely analytical script.
SCAN_DIRS_FOR_SIZE = [
    os.path.expanduser('~'),  # Your home directory
    '/Library/Caches',
    '/private/var/folders', # Common temporary files location
    '/tmp',
]

# Directories to specifically scan for large/old/cache/temp files.
# These are usually places where large unnecessary files accumulate.
SCAN_DIRS_FOR_SUGGESTIONS = [
    os.path.expanduser('~'),
    '/Library/Caches',
    '/private/var/folders',
    '/tmp',
    # Consider adding other common locations like /Applications if you want to see large apps,
    # but be mindful of permissions and relevance.
]

# Files larger than this (in MB) will be flagged as "large".
MIN_LARGE_FILE_SIZE_MB = 100

# Files older than this many days will be flagged as "old".
OLD_FILE_DAYS = 90

# Keywords/patterns to identify potential cache, temp, or log files.
# These are case-insensitive.
CACHE_PATTERNS = ['cache', '.cache', 'chromium', 'vscode', 'npm', 'yarn', 'brew', 'library/developer/xcode/deriveddata']
TEMP_PATTERNS = ['temp', '.tmp', 'temporary', 'downloads', 'trash', '.Trash']
LOG_PATTERNS = ['log', '.log', 'logs']

# Number of top directories to display
TOP_N_DIRS = 15

# --- Helper Functions ---

def convert_bytes_to_human_readable(num_bytes):
    """Converts a number of bytes into a human-readable string (e.g., 10GB, 500MB)."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB" # Fallback for extremely large sizes

def convert_human_readable_to_bytes(size_str):
    """Converts a human-readable size string (e.g., '1.2G', '500M') to bytes."""
    size_str = size_str.strip().upper()
    if not size_str:
        return 0

    num = float("".join(filter(str.isdigit, size_str)))
    unit = "".join(filter(str.isalpha, size_str))

    if 'K' in unit:
        return num * 1024
    elif 'M' in unit:
        return num * 1024**2
    elif 'G' in unit:
        return num * 1024**3
    elif 'T' in unit:
        return num * 1024**4
    elif 'P' in unit:
        return num * 1024**5
    elif 'B' in unit:
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
        return False # Cannot determine age

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

# --- Main Scan Functions ---

def get_disk_summary():
    """Gets overall disk usage for the root partition."""
    try:
        total, used, free = shutil.disk_usage("/")
        print("\n--- Disk Usage Summary (Root Partition) ---")
        print(f"Total: {convert_bytes_to_human_readable(total)}")
        print(f"Used:  {convert_bytes_to_human_readable(used)}")
        print(f"Free:  {convert_bytes_to_human_readable(free)}")
        print(f"Usage: {used / total:.2%}")
    except Exception as e:
        print(f"Error getting disk summary: {e}")

def get_directory_sizes(paths):
    """
    Uses 'du -sh' to get human-readable sizes of specified directories.
    Returns a dictionary of {path: size_in_bytes}.
    """
    print("\n--- Top Directory Sizes ---")
    print(f"Scanning directories: {', '.join(paths)}")
    dir_sizes = {}
    for path in paths:
        if not os.path.exists(path):
            print(f"Warning: Directory not found: {path}. Skipping.")
            continue
        try:
            # -s: summarize (only total for each argument)
            # -h: human-readable (not ideal for parsing, but we'll convert)
            # -k: block size of 1024 bytes (optional, but consistent if parsing numbers directly)
            # -d 0: only show size of the top-level directory itself, not sub-directories
            process = subprocess.run(
                ['du', '-sh', path],
                capture_output=True,
                text=True,
                check=True
            )
            output = process.stdout.strip().split('\t')
            if len(output) == 2:
                size_str, dir_path = output
                dir_sizes[dir_path] = convert_human_readable_to_bytes(size_str)
            else:
                print(f"Warning: Could not parse du output for {path}: {output}")
        except subprocess.CalledProcessError as e:
            print(f"Error running 'du' for {path}: {e.stderr.strip()}")
        except Exception as e:
            print(f"An unexpected error occurred for {path}: {e}")

    # Sort directories by size
    sorted_dir_sizes = sorted(dir_sizes.items(), key=lambda item: item[1], reverse=True)

    print(f"\nTop {TOP_N_DIRS} largest directories in specified scan paths:")
    for i, (path, size) in enumerate(sorted_dir_sizes[:TOP_N_DIRS]):
        print(f"  {convert_bytes_to_human_readable(size):<10} {path}")

def find_large_and_old_files_for_suggestions(directories):
    """
    Scans specified directories for large, old, or classified files.
    """
    print(f"\n--- Potential Cleanup Suggestions ---")
    print(f"Looking for files > {MIN_LARGE_FILE_SIZE_MB}MB or older than {OLD_FILE_DAYS} days in:")
    for d in directories:
        print(f"  - {d}")

    suggestions = collections.defaultdict(list)
    min_size_bytes = MIN_LARGE_FILE_SIZE_MB * 1024 * 1024

    for directory in directories:
        if not os.path.exists(directory):
            print(f"Warning: Suggestion scan directory not found: {directory}. Skipping.")
            continue
        print(f"Scanning {directory}...")
        for root, dirs, files in os.walk(directory):
            # Prune directories that are likely permission-denied or irrelevant for this scan
            dirs[:] = [d for d in dirs if not d.startswith('.')] # Skip hidden system folders
            if 'Library/Containers' in root and os.path.expanduser('~') in root:
                # Many app sandboxes are in here, often restricted or not relevant for manual cleanup
                # unless specifically targeting an app's data. Prune deep dives.
                # However, /Library/Caches is explicitly scanned.
                if 'Containers' in dirs: dirs.remove('Containers')


            for name in files:
                filepath = os.path.join(root, name)
                try:
                    # Resolve symbolic links to get actual file size
                    # and avoid double counting or following broken links
                    if os.path.islink(filepath):
                        actual_path = os.path.realpath(filepath)
                        if not os.path.exists(actual_path):
                            continue # Broken link
                        file_size = os.path.getsize(actual_path)
                    else:
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
                            suggestions[tuple(sorted(suggestion_type))].append((filepath, file_size))

                except PermissionError:
                    # print(f"Permission denied: {filepath}")
                    pass # Silently skip permission errors for individual files
                except FileNotFoundError:
                    pass # File might have been deleted between os.walk and os.path.getsize
                except Exception as e:
                    # print(f"Error processing {filepath}: {e}")
                    pass # Catch other unexpected errors

    if not suggestions:
        print("No specific cleanup suggestions found based on current criteria.")
        return

    # Sort and display suggestions
    print("\n--- Suggested Files/Directories for Review ---")
    print("  (No files will be deleted by this script. Review manually.)")
    print("  ----------------------------------------------------------")

    # Order by combined type for better readability
    ordered_suggestion_types = [
        ('Large', 'Old', 'Temporary'),
        ('Large', 'Temporary'),
        ('Old', 'Temporary'),
        ('Temporary',),
        ('Large', 'Old', 'Cache'),
        ('Large', 'Cache'),
        ('Old', 'Cache'),
        ('Cache',),
        ('Large', 'Old'),
        ('Large',),
        ('Old',),
    ]

    displayed_count = 0
    MAX_SUGGESTIONS_PER_CATEGORY = 10 # Limit output to prevent overwhelming the user

    for s_type_tuple in ordered_suggestion_types:
        s_type_name = " ".join(s_type_tuple)
        if s_type_tuple in suggestions:
            items = sorted(suggestions[s_type_tuple], key=lambda x: x[1], reverse=True)
            print(f"\n{s_type_name} Files/Folders:")
            for filepath, size in items[:MAX_SUGGESTIONS_PER_CATEGORY]:
                print(f"  {convert_bytes_to_human_readable(size):<10} {filepath}")
                displayed_count += 1
            if len(items) > MAX_SUGGESTIONS_PER_CATEGORY:
                print(f"  ... and {len(items) - MAX_SUGGESTIONS_PER_CATEGORY} more {s_type_name.lower()} files.")

    if displayed_count == 0:
        print("No specific cleanup suggestions found based on current criteria.")


# --- Main Execution ---

def main():
    print("MacBook Disk Usage Analyzer")
    print("----------------------------")
    print("NOTE: This script ONLY analyzes and suggests. It DOES NOT delete or modify any files.")
    print("Permissions: For full system scans, you might need 'sudo'. However, it's safer to run")
    print("             without 'sudo' to avoid scanning system-critical directories, which are")
    print("             usually not the source of user-related disk space issues.")

    get_disk_summary()
    get_directory_sizes(SCAN_DIRS_FOR_SIZE)
    find_large_and_old_files_for_suggestions(SCAN_DIRS_FOR_SUGGESTIONS)

    print("\nAnalysis Complete!")
    print("-------------------")
    print("Suggestions are for your review. Always be careful when deleting files.")
    print("Common locations for manual cleanup might include: Downloads folder, old application installers (.dmg),")
    print("old Xcode Derived Data, Docker images, large virtual machine files, etc.")

if __name__ == "__main__":
    main()