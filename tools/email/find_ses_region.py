import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session()

ses_regions = ['us-east-1', 'us-west-2', 'eu-west-1', 'us-east-2', 'us-west-1', 'eu-central-1']

for region in ses_regions:
    try:
        ses = session.client('ses', region_name=region)
        ids = ses.list_identities()
        if ids['Identities']:
            print(f"{region}: {ids['Identities']}")
    except Exception as e:
        print(f"{region}: error - {e}")

# Also try SESv2 in us-west-2 (where the console URL pointed)
print()
print("=== SESv2 us-west-2 ===")
sesv2 = session.client('sesv2', region_name='us-west-2')
try:
    ids = sesv2.list_email_identities()
    for i in ids.get('EmailIdentities', []):
        print(f"  {i['IdentityName']}: {i['IdentityType']}, sending={i.get('SendingEnabled')}, verified={i.get('VerificationStatus')}")
except Exception as e:
    print(f"  Error: {e}")

# Try creating domain in us-west-2 with SES v1
print()
print("=== SES v1 create domain in us-west-2 ===")
ses1 = session.client('ses', region_name='us-west-2')
try:
    resp = ses1.verify_domain_dkim(Domain='exzilcalanza.info')
    print("DKIM tokens:", resp['DkimTokens'])
except Exception as e:
    print(f"Error: {e}")
