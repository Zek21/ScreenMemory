import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session(region='us-west-2')

ses = session.client('ses')

email = 'exzilcalanza@gmail.com'
try:
    ses.verify_email_identity(EmailAddress=email)
    print(f"Verification email sent to {email}!")
    print("Check your Gmail inbox for the AWS verification link and click it.")
except Exception as e:
    print(f"Error: {e}")
