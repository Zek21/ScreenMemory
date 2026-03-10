import smtplib, ssl, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import smtp_creds

smtp_server = 'smtp.mail.us-east-1.awsapps.com'
port = 465

accounts = [
    'mail@exzilcalanza.info',
    'ops.director@exzilcalanza.info',
]

context = ssl.create_default_context()
for account in accounts:
    username, password = smtp_creds(account)
    try:
        with smtplib.SMTP_SSL(smtp_server, port, context=context) as server:
            server.login(username, password)
            print(f'LOGIN SUCCESS: {username}')
    except Exception as e:
        print(f'FAILED {username}: {e}')
