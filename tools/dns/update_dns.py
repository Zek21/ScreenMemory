import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from credentials import aws_session, r53_zone_id

session = aws_session(region='us-west-2')

r53 = session.client('route53', region_name='us-east-1')
ZONE_ID = r53_zone_id()

# Old DKIM records to DELETE
old_dkim = [
    ('3v2fs5ym3komusyuwcdzjhbn5nxeik6e._domainkey.exzilcalanza.info', '3v2fs5ym3komusyuwcdzjhbn5nxeik6e.dkim.amazonses.com'),
    ('cxcqr443r2owypuhkrywvgozoa5vslvh._domainkey.exzilcalanza.info', 'cxcqr443r2owypuhkrywvgozoa5vslvh.dkim.amazonses.com'),
    ('ljdmfqq2vrflwfvg6w64swif6il7fypn._domainkey.exzilcalanza.info', 'ljdmfqq2vrflwfvg6w64swif6il7fypn.dkim.amazonses.com'),
]

# New DKIM records to CREATE
new_dkim = [
    ('iqmc6fkcfn45huv5c2q2z4ojs4zxr7lx._domainkey.exzilcalanza.info', 'iqmc6fkcfn45huv5c2q2z4ojs4zxr7lx.dkim.amazonses.com'),
    ('oyyw74rdgyobpqkyz2o2mmytd32bbard._domainkey.exzilcalanza.info', 'oyyw74rdgyobpqkyz2o2mmytd32bbard.dkim.amazonses.com'),
    ('sgdduhhtyq7fxkesxfdda2mfjhdhsncj._domainkey.exzilcalanza.info', 'sgdduhhtyq7fxkesxfdda2mfjhdhsncj.dkim.amazonses.com'),
]

changes = []

# First get exact existing records to match TTL
existing = r53.list_resource_record_sets(HostedZoneId=ZONE_ID)
existing_map = {}
for r in existing['ResourceRecordSets']:
    existing_map[r['Name'].rstrip('.')] = r

# Delete old DKIM using exact existing values
for name, value in old_dkim:
    key = name.rstrip('.')
    if key in existing_map:
        changes.append({
            'Action': 'DELETE',
            'ResourceRecordSet': existing_map[key]
        })

# Create new DKIM
for name, value in new_dkim:
    changes.append({
        'Action': 'CREATE',
        'ResourceRecordSet': {
            'Name': name,
            'Type': 'CNAME',
            'TTL': 300,
            'ResourceRecords': [{'Value': value}]
        }
    })

try:
    response = r53.change_resource_record_sets(
        HostedZoneId=ZONE_ID,
        ChangeBatch={
            'Comment': 'Update DKIM records for SES domain verification',
            'Changes': changes
        }
    )
    print(f"SUCCESS! Change ID: {response['ChangeInfo']['Id']}")
    print(f"Status: {response['ChangeInfo']['Status']}")
    print(f"Deleted {len(old_dkim)} old DKIM records")
    print(f"Created {len(new_dkim)} new DKIM records")
    print("DNS propagation may take a few minutes. SES will auto-verify once records are live.")
except Exception as e:
    print(f"FAILED: {e}")
