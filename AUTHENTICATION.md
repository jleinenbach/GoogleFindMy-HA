# Google Find My Device Authentication Setup

This integration requires proper authentication tokens from Google's Find My Device service. Since Google's authentication requires Chrome browser access, you need to generate these tokens on a machine with Chrome installed.

## Authentication Method: Using GoogleFindMyTools

1. **Download the original GoogleFindMyTools**:
   - Go to https://github.com/leonboe1/GoogleFindMyTools/
   - Download or clone the repository

2. **Run the authentication script**:
   - Install Python dependencies: `pip install gpsoauth selenium undetected-chromedriver`
   - Run the main script on a machine with Chrome installed
   - Complete the Google authentication process in the browser

3. **Extract the secrets.json file**:
   - After successful authentication, find the `Auth/secrets.json` file
   - The file should contain tokens like: `aas_token`, `username`, `adm_token_[email]`, etc.
   - Copy the entire contents of this JSON file (it should be valid JSON format)

4. **Configure in Home Assistant**:
   - Add the Google Find My Device integration
   - Paste the contents of `secrets.json` into the "Secrets.json Content" field

## Important Notes

- **Chrome Required**: The authentication process requires Google Chrome browser
- **Security**: Keep your authentication tokens secure and don't share them
- **Expiration**: Tokens may expire and need to be regenerated periodically
- **Find My Device**: Make sure Find My Device is enabled on your Android devices

## Troubleshooting

- If you get "401 Unauthorized" errors, your tokens may be expired or invalid
- If you get "BadAuthentication" errors, check your Google email and token format
- For "Invalid JSON" errors, ensure you're copying the complete secrets.json content

## Support

This integration is based on the GoogleFindMyTools project. For authentication issues, refer to the original project documentation at https://github.com/leonboe1/GoogleFindMyTools/