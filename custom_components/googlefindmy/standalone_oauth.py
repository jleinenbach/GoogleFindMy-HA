#!/usr/bin/env python3
"""
Standalone OAuth token generator for Google Find My Device.
This script contains all necessary code to work independently.
"""

def main():
    """Get OAuth token for Google Find My Device."""
    print("=" * 60)
    print("Google Find My Device - OAuth Token Generator")
    print("=" * 60)
    print()
    
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.support.ui import WebDriverWait
        import os
        import shutil
        import platform
        
        def find_chrome():
            """Find Chrome executable using known paths and system commands."""
            possiblePaths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\ProgramData\chocolatey\bin\chrome.exe",
                r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
                "/usr/bin/google-chrome",
                "/usr/local/bin/google-chrome",
                "/opt/google/chrome/chrome",
                "/snap/bin/chromium",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ]

            # Check predefined paths
            for path in possiblePaths:
                if os.path.exists(path):
                    return path

            # Use system command to find Chrome
            try:
                if platform.system() == "Windows":
                    chrome_path = shutil.which("chrome")
                else:
                    chrome_path = shutil.which("google-chrome") or shutil.which("chromium")
                if chrome_path:
                    return chrome_path
            except Exception as e:
                print(f"Error while searching system paths: {e}")

            return None

        def create_driver(headless=False):
            """Create and configure Chrome driver."""
            chrome_executable = find_chrome()
            
            if chrome_executable is None:
                raise Exception("Chrome/Chromium not found. Please install Google Chrome or Chromium.")

            options = uc.ChromeOptions()
            options.binary_location = chrome_executable
            
            if headless:
                options.add_argument("--headless")
            
            # Additional options for better compatibility
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--remote-debugging-port=9222")
            
            return uc.Chrome(options=options)

        print("This script will open Chrome to authenticate with Google.")
        print("After logging in, the OAuth token will be displayed.")
        print("Press Enter to continue...")
        input()
        
        print("Opening Chrome browser...")
        driver = create_driver(headless=False)

        try:
            # Open the browser and navigate to the URL
            driver.get("https://accounts.google.com/EmbeddedSetup")

            # Wait until the "oauth_token" cookie is set
            print("Waiting for authentication... Please complete the login process in the browser.")
            WebDriverWait(driver, 300).until(
                lambda d: d.get_cookie("oauth_token") is not None
            )

            # Get the value of the "oauth_token" cookie
            oauth_token_cookie = driver.get_cookie("oauth_token")
            oauth_token_value = oauth_token_cookie['value']

            if oauth_token_value:
                print()
                print("=" * 60)
                print("SUCCESS! Your OAuth token is:")
                print("=" * 60)
                print(oauth_token_value)
                print("=" * 60)
                print()
                print("Copy this token and paste it in Home Assistant when")
                print("configuring the Google Find My Device integration.")
                print("Choose 'Manual Token Entry' as the authentication method.")
                print()
                return oauth_token_value
            else:
                print("Failed to obtain OAuth token.")
                return None

        finally:
            # Close the browser
            driver.quit()
            
    except ImportError as e:
        print(f"Missing required package: {e}")
        print()
        print("Please install the required packages:")
        print("pip install selenium undetected-chromedriver")
        return None
        
    except Exception as e:
        print(f"Error: {e}")
        print()
        print("Make sure you have Chrome installed and try again.")
        return None

if __name__ == "__main__":
    main()