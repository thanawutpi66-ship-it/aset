import os

search_dir = r"d:\ASET\aset\aset_batt"
query = "sig_profile_status.emit"

for root, _, files in os.walk(search_dir):
    for f in files:
        if f.endswith(".py"):
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as file:
                    for i, line in enumerate(file, 1):
                        if query in line:
                            print(f"{path}:{i}: {line.strip()}")
            except Exception:
                pass
