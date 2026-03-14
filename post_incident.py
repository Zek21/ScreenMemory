# Script to post INCIDENT 013 to the bus using guarded_publish
from tools.skynet_spam_guard import guarded_publish

msg = {
    'sender': 'delta',
    'topic': 'orchestrator',
    'type': 'result',
    'content': 'INCIDENT 013 (VS Code Quickpick Unreachable via Win32 APIs) added to data/incidents.json. signed:delta'
}

result = guarded_publish(msg)
print('Publish result:', result)
