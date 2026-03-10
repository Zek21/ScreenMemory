import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session(region='us-west-2')

ses = session.client('ses')

print('=== Verified Identities ===')
ids = ses.list_identities()
for i in ids['Identities']:
    attrs = ses.get_identity_verification_attributes(Identities=[i])
    status = attrs['VerificationAttributes'].get(i, {}).get('VerificationStatus', 'unknown')
    print(f'  {i}: {status}')

print()
print('=== Send Quota ===')
quota = ses.get_send_quota()
print(f"  Max24h: {quota['Max24HourSend']}, SentLast24h: {quota['SentLast24Hours']}, MaxSendRate: {quota['MaxSendRate']}")

print()
print('=== Account Sending Enabled ===')
try:
    enabled = ses.get_account_sending_enabled()
    print(f"  Enabled: {enabled['Enabled']}")
except Exception as e:
    print(f'  Error: {e}')
