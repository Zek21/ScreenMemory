import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from credentials import aws_session

session = aws_session(region='us-west-2')

r53 = session.client('route53', region_name='us-east-1')
resp = r53.get_change(Id='/change/C028711931DJ2WU1JEY5Z')
print("DNS change status:", resp["ChangeInfo"]["Status"])

sesv2 = session.client('sesv2')
ident = sesv2.get_email_identity(EmailIdentity='exzilcalanza.info')
print("Domain verified for sending:", ident["VerifiedForSendingStatus"])
print("DKIM status:", ident["DkimAttributes"]["Status"])
print("DKIM signing enabled:", ident["DkimAttributes"]["SigningEnabled"])

gmail = sesv2.get_email_identity(EmailIdentity='exzilcalanza@gmail.com')
print("Gmail verified:", gmail["VerifiedForSendingStatus"])
