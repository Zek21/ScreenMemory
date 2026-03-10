"""Generate final Austria prospects markdown file."""
import csv
from collections import defaultdict

INPUT = r'd:\Prospects\Site\results\austria\austria_final_no_website.csv'
OUTPUT = r'd:\Prospects\Site\results\austria\austria_prospects_with_email.md'

rows = list(csv.DictReader(open(INPUT, encoding='utf-8')))

# Group by city
by_city = defaultdict(list)
for r in rows:
    by_city[r['city']].append(r)

# Sort cities by count
sorted_cities = sorted(by_city.items(), key=lambda x: -len(x[1]))

with open(OUTPUT, 'w', encoding='utf-8') as f:
    f.write("# Austria Prospects - Businesses Without Websites\n\n")
    f.write(f"**Total: {len(rows)} verified prospects** across {len(by_city)} cities\n\n")
    f.write("These businesses were found on Google Maps without a website listed, ")
    f.write("and their email domains were verified to NOT have a live website.\n\n")
    f.write("---\n\n")
    
    for city, businesses in sorted_cities:
        f.write(f"## {city} ({len(businesses)} businesses)\n\n")
        
        for b in sorted(businesses, key=lambda x: x.get('search_category', '')):
            name = b['name']
            email = b.get('email', '')
            phone = b.get('phone', '')
            address = b.get('address', '')
            category = b.get('search_category', '') or b.get('google_category', '')
            rating = b.get('rating', '')
            reviews = b.get('reviews', '')
            facebook = b.get('facebook', '')
            
            f.write(f"### {name}\n")
            f.write(f"- **Category:** {category}\n")
            f.write(f"- **Email:** {email}\n")
            if phone:
                f.write(f"- **Phone:** {phone}\n")
            if address:
                f.write(f"- **Address:** {address}\n")
            if rating and rating != '0':
                review_str = f" ({reviews} reviews)" if reviews and reviews != '0' else ""
                f.write(f"- **Rating:** {rating}⭐{review_str}\n")
            if facebook:
                f.write(f"- **Facebook:** [{facebook}]({facebook})\n")
            f.write("\n")
        
        f.write("---\n\n")

print(f"Generated MD with {len(rows)} prospects across {len(by_city)} cities")
print(f"Saved to: {OUTPUT}")
