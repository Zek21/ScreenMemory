import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session(region='us-west-2')

# SES v2 client has more account details
sesv2 = session.client('sesv2')

print('=== SES Account Details (v2 API) ===')
try:
    account = sesv2.get_account()
    print(f"  Production Access: {account.get('ProductionAccessEnabled', 'unknown')}")
    print(f"  Sending Enabled: {account.get('SendingEnabled', 'unknown')}")
    print(f"  Enforcement Status: {account.get('EnforcementStatus', 'unknown')}")
    sq = account.get('SendQuota', {})
    print(f"  Max24HourSend: {sq.get('Max24HourSend', 'unknown')}")
    print(f"  MaxSendRate: {sq.get('MaxSendRate', 'unknown')}")
    print(f"  SentLast24Hours: {sq.get('SentLast24Hours', 'unknown')}")
    print()
    print('  Full account details:')
    for k, v in account.items():
        if k != 'ResponseMetadata':
            print(f"    {k}: {v}")
except Exception as e:
    print(f"  Error: {e}")

print()
print('=== All Verified Identities ===')
try:
    identities = sesv2.list_email_identities()
    for ident in identities.get('EmailIdentities', []):
        print(f"  {ident['IdentityName']}: type={ident['IdentityType']}, sending={ident.get('SendingEnabled', '?')}, verified={ident.get('VerificationStatus', '?')}")
except Exception as e:
    print(f"  Error: {e}")
