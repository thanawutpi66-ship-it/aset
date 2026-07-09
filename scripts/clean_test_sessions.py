import os
import glob

def clean_sessions(sessions_dir="sessions"):
    if not os.path.isdir(sessions_dir):
        print(f"Directory {sessions_dir} not found.")
        return

    csv_files = glob.glob(os.path.join(sessions_dir, "*.csv"))
    removed_csv = 0
    for f in csv_files:
        try:
            with open(f, 'r', encoding='utf-8-sig') as file:
                lines = file.readlines()
                # A file with just the header or a few rows is likely a test artifact
                # Also check if it's very small in bytes
                if len(lines) <= 5 or os.path.getsize(f) < 500:
                    os.remove(f)
                    removed_csv += 1
        except Exception as e:
            print(f"Error processing {f}: {e}")

    print(f"Removed {removed_csv} small/empty session CSV files (likely test artifacts).")
    
    # Clean up empty .session_meta.json if present and empty
    meta = os.path.join(sessions_dir, ".session_meta.json")
    if os.path.exists(meta) and os.path.getsize(meta) == 0:
        os.remove(meta)

if __name__ == "__main__":
    # If run from the scripts directory, adjust path to point to the root sessions dir
    if os.path.basename(os.getcwd()) == "scripts":
        clean_sessions("../sessions")
    else:
        clean_sessions("sessions")
