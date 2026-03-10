import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from credentials import aws_session, r53_zone_id

session = aws_session(region='us-east-1')

r53 = session.client('route53', region_name='us-east-1')
ZONE_ID = r53_zone_id()

# Update _amazonses TXT record with new verification token for us-west-2
# Keep existing record and add new value
changes = [
    {
        'Action': 'UPSERT',
        'ResourceRecordSet': {
            'Name': '_amazonses.exzilcalanza.info',
            'Type': 'TXT',
            'TTL': 300,
            'ResourceRecords': [
                {'Value': '"tdlgrzGZRyn9YdDzkkqdWOfxZz10icCJJ3LPwE7Z9LE="'},
                {'Value': '"TFybV0ey5VaQ4qgVq72ATlbvLQxetF4Mbh1sEKuHoLY="'},
            ]
        }
    }
]

try:
    resp = r53.change_resource_record_sets(
        HostedZoneId=ZONE_ID,
        ChangeBatch={
            'Comment': 'Add SES domain verification token for us-west-2',
            'Changes': changes
        }
    )
    print(f"SUCCESS! Status: {resp['ChangeInfo']['Status']}")
except Exception as e:
    print(f"Failed: {e}")
