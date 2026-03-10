import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session(region='us-west-2')

ses = session.client('ses')

# Verify domain identity
print("Verifying domain exzilcalanza.info...")
resp = ses.verify_domain_identity(Domain='exzilcalanza.info')
print(f"Verification token: {resp['VerificationToken']}")

# Check domain DKIM status
resp2 = ses.get_identity_dkim_attributes(Identities=['exzilcalanza.info'])
attrs = resp2['DkimAttributes'].get('exzilcalanza.info', {})
print(f"DKIM enabled: {attrs.get('DkimEnabled')}")
print(f"DKIM status: {attrs.get('DkimVerificationStatus')}")

# Check DNS change status
r53 = session.client('route53', region_name='us-east-1')
resp3 = r53.get_change(Id='/change/C028711931DJ2WU1JEY5Z')
print(f"DNS change: {resp3['ChangeInfo']['Status']}")

# Check all identities
print()
print("=== Current verification status ===")
ids = ses.list_identities()
attrs = ses.get_identity_verification_attributes(Identities=ids['Identities'])
for identity, info in attrs['VerificationAttributes'].items():
    print(f"  {identity}: {info['VerificationStatus']}")

# Now try sending the test email (mail@ is already verified)
print()
print("=== Attempting to send email ===")
try:
    response = ses.send_email(
        Source="Exzil Calanza <mail@exzilcalanza.info>",
        Destination={'ToAddresses': ['exzilcalanza@gmail.com']},
        Message={
            'Subject': {'Data': '[TEST] From SES - MATOKA Draft', 'Charset': 'UTF-8'},
            'Body': {
                'Text': {'Data': 'This is a test email from SES to verify delivery works.', 'Charset': 'UTF-8'},
            }
        }
    )
    print(f"SENT! MessageId: {response['MessageId']}")
except Exception as e:
    print(f"Send failed: {e}")
