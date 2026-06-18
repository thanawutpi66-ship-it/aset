"""
Main entry point for ASET Battery Characterization System
"""
import sys
import os

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    """Main application entry point"""
    try:
        from app_bootstrapper import create_application
        create_application()
    except ImportError as e:
        print(f"Import error: {e}")
        print("Please ensure all required modules are installed")
        sys.exit(1)
    except Exception as e:
        print(f"Application error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()