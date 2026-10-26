"""Strata Cloud Manager service account settings.

Fill in the three values below with the service account credentials and the
Tenant Service Group (TSG) ID of the tenant you want to export from or import to.

How to get them:
  1. Log in to Strata Cloud Manager (https://stratacloudmanager.paloaltonetworks.com).
  2. Settings > Identity & Access > Add Identity > Service Account.
  3. Save the Client ID and Client Secret shown at the end of the wizard
     (the secret is shown once only).
  4. Assign a role on the tenant: "View Only Administrator" is enough for
     config_export.py; config_import.py needs "Superuser" or "Network Administrator".
  5. The TSG ID is shown in Settings > Tenants (the numeric ID of the tenant).

Keep this file private. Do not commit real credentials to version control.
"""

# Service account client ID, for example: my-sa@1234567890.iam.panserviceaccount.com
CLIENT_ID = ""

# Service account client secret (shown once, when the service account is created)
CLIENT_SECRET = ""

# Tenant Service Group ID, for example: 1234567890
TSG_ID = ""
