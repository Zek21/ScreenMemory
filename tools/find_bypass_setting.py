"""Find VS Code state DB keys related to approval/bypass/permissions."""
import sqlite3, os, glob

vsdir = os.path.expandvars(r'%APPDATA%\Code - Insiders\User')
patterns = ['%approval%', '%permission%', '%bypass%', '%autoApprove%', '%toolApproval%', '%requestQueue%']

for db_path in glob.glob(vsdir + r'\**\state.vscdb', recursive=True):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        for pat in patterns:
            cursor.execute("SELECT key, value FROM ItemTable WHERE key LIKE ? LIMIT 10", (pat,))
            rows = cursor.fetchall()
            if rows:
                print('DB:', db_path[-90:])
                for k, v in rows:
                    vstr = str(v)[:120] if v else 'null'
                    print('  key=%s' % k)
                    print('  val=%s' % vstr[:120])
        conn.close()
    except Exception as e:
        pass

print('\nDone searching')
