import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session(region='us-west-2')

ses = session.client('ses')

SENDER = "Exzil Calanza <mail@exzilcalanza.info>"
RECIPIENT = "exzilcalanza@gmail.com"
SUBJECT = "[TEST] MATOKA — You have 2,126 fans but nowhere to send them"

BODY_HTML = """<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
<p>Hi MATOKA team,</p>

<p>I recently came across your bakery while exploring the best Georgian food spots in Prague &mdash; and your reputation is incredible. <strong>2,126 Google reviews</strong>, a <strong>4.7-star rating</strong>, and you&rsquo;ve earned the title of <em>&ldquo;Khachapuri Kings in Prague.&rdquo;</em> That&rsquo;s better than most restaurant chains with entire marketing departments.</p>

<p>But here&rsquo;s the thing: when someone Googles <strong>&ldquo;MATOKA Georgian Prague&rdquo;</strong> right now, there&rsquo;s no website to land on. No menu to browse, no online ordering, no story about your Georgian roots &mdash; just a Facebook page and a Google Maps pin. You&rsquo;re leaving customers (and revenue) on the table every single day.</p>

<p><strong>Imagine this:</strong></p>
<ul>
<li>&#127760; A beautiful, fast website showcasing your menu, story, and Vinohrady location &mdash; optimized so you show up #1 on Google when people search for Georgian food in Prague</li>
<li>&#128241; Online ordering &amp; reservations so customers can order khachapuri before they even walk in</li>
<li>&#128200; SEO &amp; traffic strategy to turn those 2,126 reviewers into repeat visitors and attract tourists searching for the best eats in Prague</li>
<li>&#128242; Social media management to keep your 1,200+ Facebook followers engaged with fresh content, reels, and promotions</li>
</ul>

<p>I&rsquo;m Exzil Calanza &mdash; I build websites and digital presence for growing businesses. You can see my work at <a href="https://exzilcalanza.info">exzilcalanza.info</a>. I specialize in helping businesses like yours go from word-of-mouth legends to online powerhouses.</p>

<p><strong>Here&rsquo;s what I&rsquo;d propose to start:</strong></p>
<ol>
<li>A free 30-minute consultation to map out your digital strategy</li>
<li>A custom website mockup &mdash; no commitment, just to show you what&rsquo;s possible</li>
<li>A clear plan for driving traffic and online orders within 90 days</li>
</ol>

<p>You&rsquo;ve already done the hardest part &mdash; building a product people love. Let me help the internet catch up to your reputation.</p>

<p>Would you be open to a quick call this week? I&rsquo;m flexible on time and happy to work around your schedule.</p>

<p>Best regards,<br>
<strong>Exzil Calanza</strong><br>
&#127760; <a href="https://exzilcalanza.info">exzilcalanza.info</a><br>
&#128231; <a href="mailto:mail@exzilcalanza.info">mail@exzilcalanza.info</a></p>
</body>
</html>"""

BODY_TEXT = """Hi MATOKA team,

I recently came across your bakery while exploring the best Georgian food spots in Prague - and your reputation is incredible. 2,126 Google reviews, a 4.7-star rating, and you've earned the title of "Khachapuri Kings in Prague." That's better than most restaurant chains with entire marketing departments.

But here's the thing: when someone Googles "MATOKA Georgian Prague" right now, there's no website to land on. No menu to browse, no online ordering, no story about your Georgian roots - just a Facebook page and a Google Maps pin. You're leaving customers (and revenue) on the table every single day.

Imagine this:
- A beautiful, fast website showcasing your menu, story, and Vinohrady location - optimized so you show up #1 on Google when people search for Georgian food in Prague
- Online ordering & reservations so customers can order khachapuri before they even walk in
- SEO & traffic strategy to turn those 2,126 reviewers into repeat visitors and attract tourists searching for the best eats in Prague
- Social media management to keep your 1,200+ Facebook followers engaged with fresh content, reels, and promotions

I'm Exzil Calanza - I build websites and digital presence for growing businesses. You can see my work at exzilcalanza.info. I specialize in helping businesses like yours go from word-of-mouth legends to online powerhouses.

Here's what I'd propose to start:
1. A free 30-minute consultation to map out your digital strategy
2. A custom website mockup - no commitment, just to show you what's possible
3. A clear plan for driving traffic and online orders within 90 days

You've already done the hardest part - building a product people love. Let me help the internet catch up to your reputation.

Would you be open to a quick call this week? I'm flexible on time and happy to work around your schedule.

Best regards,
Exzil Calanza
exzilcalanza.info
mail@exzilcalanza.info"""

try:
    response = ses.send_email(
        Source=SENDER,
        Destination={'ToAddresses': [RECIPIENT]},
        Message={
            'Subject': {'Data': SUBJECT, 'Charset': 'UTF-8'},
            'Body': {
                'Text': {'Data': BODY_TEXT, 'Charset': 'UTF-8'},
                'Html': {'Data': BODY_HTML, 'Charset': 'UTF-8'},
            }
        }
    )
    print(f"SUCCESS! MessageId: {response['MessageId']}")
    print(f"Sent from: {SENDER}")
    print(f"Sent to: {RECIPIENT}")
except Exception as e:
    print(f"FAILED: {e}")
