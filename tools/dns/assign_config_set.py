import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from credentials import aws_session

session = aws_session(region='us-west-2')
ses = session.client('sesv2')

# List configuration sets
configs = ses.list_configuration_sets()
print('Configuration Sets:')
for cs in configs.get('ConfigurationSets', []):
    print(f'  - {cs}')

# Check current identity status
identity = ses.get_email_identity(EmailIdentity='exzilcalanza.info')
print(f'\nIdentity: exzilcalanza.info')
print(f'  Status: {identity.get("VerificationStatus")}')
print(f'  Config Set: {identity.get("ConfigurationSetName", "NONE")}')
print(f'  DKIM: {identity.get("DkimAttributes", {}).get("Status")}')

# Assign configuration set to the identity
config_set_name = 'my-first-configuration-set'
print(f'\nAssigning "{config_set_name}" to exzilcalanza.info...')
ses.put_email_identity_configuration_set_attributes(
    EmailIdentity='exzilcalanza.info',
    ConfigurationSetName=config_set_name
)
print('Done!')

# Verify
identity2 = ses.get_email_identity(EmailIdentity='exzilcalanza.info')
print(f'  Config Set now: {identity2.get("ConfigurationSetName", "NONE")}')
