import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session(region='us-east-1')

sesv2 = session.client('sesv2')

# Delete old failed identity and re-create it to get fresh DKIM verification
print("Deleting old domain identity...")
try:
    sesv2.delete_email_identity(EmailIdentity='exzilcalanza.info')
    print("Deleted.")
except Exception as e:
    print(f"Delete error: {e}")

print("Re-creating domain identity with DKIM...")
try:
    resp = sesv2.create_email_identity(
        EmailIdentity='exzilcalanza.info',
        DkimSigningAttributes={
            'DomainSigningAttributesOrigin': 'AWS_SES'
        }
    )
    print("Created! DKIM status:", resp["DkimAttributes"]["Status"])
    for t in resp["DkimAttributes"].get("Tokens", []):
        print(f"  DKIM Token: {t}")
    print()
    print("New CNAME records needed:")
    for t in resp["DkimAttributes"].get("Tokens", []):
        print(f"  {t}._domainkey.exzilcalanza.info -> {t}.dkim.amazonses.com")
except Exception as e:
    print(f"Create error: {e}")
