import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from credentials import aws_session

session = aws_session(region='us-west-2')

# Check if Route 53 manages the domain
r53 = session.client('route53', region_name='us-east-1')

print('=== Route 53 Hosted Zones ===')
try:
    zones = r53.list_hosted_zones()
    for z in zones['HostedZones']:
        print(f"  {z['Name']} - ID: {z['Id']}")
        if 'exzilcalanza' in z['Name']:
            zone_id = z['Id'].split('/')[-1]
            print(f"  >>> Found! Zone ID: {zone_id}")
            records = r53.list_resource_record_sets(HostedZoneId=zone_id)
            print(f"  Current records:")
            for r in records['ResourceRecordSets']:
                vals = [v['Value'] for v in r.get('ResourceRecords', [])]
                alias = r.get('AliasTarget', {}).get('DNSName', '')
                print(f"    {r['Type']:6s} {r['Name']:50s} {', '.join(vals) if vals else alias}")
except Exception as e:
    print(f"  Error: {e}")
