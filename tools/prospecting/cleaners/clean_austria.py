"""Clean Austria email results - remove false positives."""
import csv

INPUT = r'd:\Prospects\Site\results\austria\_intermediate\austria_emails_fb.csv'
OUTPUT = r'd:\Prospects\Site\results\austria\_intermediate\austria_emails_clean.csv'

rows = list(csv.DictReader(open(INPUT, encoding='utf-8')))
print(f"Input: {len(rows)} rows")

# Emails to remove - wrong FB page matches, wrong country, corporate, duplicates
BAD_EMAILS = {
    'info@broadwayhair.co.uk',       # UK email for Klagenfurt business
    'info@rahnsdorfer-blumenwelt.de', # German email for Wels business
    'info@gaschnitz-autoservice.de',  # German email for St. Pölten business
    'haarstudio-manuela@t-online.de', # German email for St. Pölten business
    'biuro@kfztechnik.pl',            # Polish email for Austrian businesses
    'info@colortronix.de',            # German email for Innsbruck business
    'info@blumenfrisch.ch',           # Swiss email for Linz business
    'rdevaney@drycleaningbydorothy.com', # US email for Linz dry cleaner
    'info@cleancar.pl',               # Polish email for Salzburg business
    'serviceclients@beautynails.com',  # Corporate/French email
    'info@beautystudioanoeskadewit.nl', # Dutch email for Salzburg business
    'sun_beauty_lounge@t-online.de',   # German email for Klagenfurt business
    'blumenbinderei@freenet.de',       # German email for Salzburg business
    'lewis@topwash.uk',                # UK email for Salzburg business
    'info@autowaschen-wendt.de',       # German email for Innsbruck business
    'beautyloungegalway@gmail.com',    # Galway Ireland for Salzburg business
    'info@weisheit-blumen.de',         # German email for Dornbirn business
    'info@3dfactory.co.il',            # Israeli email for Linz business
    'sven.nesselberger@3druckschmiede.de', # German email for Dornbirn business
    'info@kfz-meisterbetrieb-cinar.de',   # German email for Salzburg business
    'repuestosacs@hotmail.com',        # Spanish-named for Villach business
    'dali.barbershopkiev@gmail.com',   # Kiev reference for Dornbirn business
    'diegoferreyra.coaching@gmail.com', # Wrong FB match for Klagenfurt business
    'somospromente@gmail.com',         # Spanish org email, wrong match
    'lavidacafeeg@gmail.com',          # Wrong FB match
    'pureluxe.nm@gmail.com',           # Wrong FB match for Villach
    'hamza@mein.gmx',                  # Malformed email (missing TLD)
    'info@piccobello.com',             # Corporate, not the local Austrian business
    'info@itsthehair.net',             # Generic, not clearly Austrian
}

# Track seen emails to remove exact duplicates (chains with same email)
seen_emails = set()
clean = []

for r in rows:
    email = r.get('email', '').lower().strip()
    
    # Skip bad emails
    if email in BAD_EMAILS:
        print(f"  REMOVED (bad): {r['name']} ({r['city']}) -> {email}")
        continue
    
    # Skip if we've seen this email already (chain duplicates)
    if email in seen_emails:
        print(f"  REMOVED (dupe): {r['name']} ({r['city']}) -> {email}")
        continue
    
    seen_emails.add(email)
    clean.append(r)

print(f"\nAfter cleaning: {len(clean)} prospects")

# Save
fieldnames = list(rows[0].keys())
with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(clean)

# Show final list
print("\nFinal Austria prospects:")
for r in clean:
    print(f"  {r['name']} ({r['city']}) -> {r.get('email','')}")
