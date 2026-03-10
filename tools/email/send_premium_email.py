import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from credentials import aws_session

session = aws_session(region='us-west-2')

ses = session.client('sesv2')

html_body = """<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>MATOKA - Digital Growth Proposal</title>
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<![endif]-->
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #f0f2f5; margin: 0; padding: 0; -webkit-text-size-adjust: 100%; }
table { border-spacing: 0; border-collapse: collapse; }
img { border: 0; display: block; outline: none; text-decoration: none; }
</style>
</head>
<body style="margin:0;padding:0;background-color:#f0f2f5;">

<!-- Preheader (hidden preview text) -->
<div style="display:none;font-size:1px;color:#f0f2f5;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
  Your bakery deserves a digital presence as legendary as your khachapuri. Let's make it happen.
</div>

<!-- Full-width wrapper -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f2f5;">
<tr><td align="center" style="padding:30px 15px;">

<!-- Main container -->
<table role="presentation" width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">

<!-- ======== HERO SECTION ======== -->
<tr><td>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);border-radius:16px 16px 0 0;overflow:hidden;">
<tr><td style="padding:0;">

<!-- Top accent bar -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td style="height:4px;background:linear-gradient(90deg,#e94560,#f5a623,#e94560);font-size:0;line-height:0;">&nbsp;</td>
</tr>
</table>

<!-- Logo & header area -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td style="padding:40px 45px 15px;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr>
<td style="width:48px;height:48px;background:linear-gradient(135deg,#e94560,#f5a623);border-radius:12px;text-align:center;vertical-align:middle;">
<span style="font-size:24px;font-weight:800;color:#ffffff;line-height:48px;font-family:'Inter',sans-serif;">EC</span>
</td>
<td style="padding-left:14px;">
<p style="font-size:18px;font-weight:700;color:#ffffff;margin:0;letter-spacing:-0.3px;">Exzil Calanza</p>
<p style="font-size:12px;font-weight:400;color:#8899aa;margin:2px 0 0;letter-spacing:0.5px;text-transform:uppercase;">Digital Growth Strategist</p>
</td>
</tr>
</table>
</td></tr>
</table>

<!-- Hero text -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td style="padding:25px 45px 10px;">
<p style="font-size:13px;font-weight:600;color:#e94560;letter-spacing:2px;text-transform:uppercase;margin:0;">Exclusive Proposal</p>
</td></tr>
<tr><td style="padding:8px 45px 0;">
<h1 style="font-size:32px;font-weight:800;color:#ffffff;margin:0;line-height:1.2;letter-spacing:-0.5px;">Your Bakery Deserves<br>a Digital Empire.</h1>
</td></tr>
<tr><td style="padding:18px 45px 35px;">
<p style="font-size:15px;font-weight:400;color:#a0b0c0;margin:0;line-height:1.7;">A personalized strategy to transform MATOKA from a local legend into an online powerhouse.</p>
</td></tr>
</table>

</td></tr>
</table>
</td></tr>

<!-- ======== MAIN CONTENT ======== -->
<tr><td>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#ffffff;">

<!-- Greeting -->
<tr><td style="padding:40px 45px 0;">
<p style="font-size:16px;color:#2d3748;line-height:1.8;margin:0;">Hi <strong>MATOKA team</strong>,</p>
<p style="font-size:15px;color:#4a5568;line-height:1.8;margin:16px 0 0;">I recently came across your bakery while exploring the best Georgian food spots in Prague &mdash; and your reputation is <strong>incredible</strong>.</p>
</td></tr>

<!-- Stats Cards -->
<tr><td style="padding:30px 35px 10px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td width="33%" style="padding:0 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);border-radius:14px;overflow:hidden;">
<tr><td style="padding:24px 16px;text-align:center;">
<p style="font-size:28px;font-weight:800;color:#ffffff;margin:0;">2,126</p>
<p style="font-size:11px;font-weight:600;color:rgba(255,255,255,0.8);margin:6px 0 0;text-transform:uppercase;letter-spacing:1px;">Google Reviews</p>
</td></tr>
</table>
</td>
<td width="33%" style="padding:0 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#f093fb 0%,#f5576c 100%);border-radius:14px;overflow:hidden;">
<tr><td style="padding:24px 16px;text-align:center;">
<p style="font-size:28px;font-weight:800;color:#ffffff;margin:0;">4.7 &#11088;</p>
<p style="font-size:11px;font-weight:600;color:rgba(255,255,255,0.8);margin:6px 0 0;text-transform:uppercase;letter-spacing:1px;">Star Rating</p>
</td></tr>
</table>
</td>
<td width="33%" style="padding:0 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#4facfe 0%,#00f2fe 100%);border-radius:14px;overflow:hidden;">
<tr><td style="padding:24px 16px;text-align:center;">
<p style="font-size:28px;font-weight:800;color:#ffffff;margin:0;">1.2K+</p>
<p style="font-size:11px;font-weight:600;color:rgba(255,255,255,0.8);margin:6px 0 0;text-transform:uppercase;letter-spacing:1px;">FB Followers</p>
</td></tr>
</table>
</td>
</tr>
</table>
</td></tr>

<!-- The Problem -->
<tr><td style="padding:30px 45px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#fff8f0;border-left:4px solid #f5a623;border-radius:0 10px 10px 0;">
<tr><td style="padding:22px 28px;">
<p style="font-size:14px;font-weight:600;color:#c05621;margin:0 0 6px;">&#9888;&#65039; The Gap in Your Growth</p>
<p style="font-size:14px;color:#744210;line-height:1.7;margin:0;">When someone Googles <strong>"MATOKA Georgian Prague"</strong> right now, there's no website to land on. No menu to browse, no online ordering, no story about your Georgian roots. You're leaving customers <em>and revenue</em> on the table every single day.</p>
</td></tr>
</table>
</td></tr>

<!-- What I'll Build - Section Header -->
<tr><td style="padding:35px 45px 0;">
<p style="font-size:13px;font-weight:700;color:#e94560;letter-spacing:2px;text-transform:uppercase;margin:0;">What I'll Build For You</p>
<h2 style="font-size:24px;font-weight:800;color:#1a202c;margin:8px 0 0;letter-spacing:-0.3px;">Imagine This</h2>
</td></tr>

<!-- Feature Cards -->
<tr><td style="padding:20px 35px 0;">
<!-- Feature 1 -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;">
<tr>
<td width="56" style="vertical-align:top;padding:0 0 0 8px;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:48px;height:48px;background:linear-gradient(135deg,#e6fffa,#b2f5ea);border-radius:12px;text-align:center;vertical-align:middle;">
<span style="font-size:22px;line-height:48px;">&#127760;</span>
</td></tr>
</table>
</td>
<td style="padding:4px 0 16px 14px;border-bottom:1px solid #f0f0f0;">
<p style="font-size:15px;font-weight:700;color:#1a202c;margin:0;">Stunning Website</p>
<p style="font-size:13px;color:#718096;line-height:1.6;margin:4px 0 0;">A beautiful, fast website showcasing your menu, story, and Vinohrady location &mdash; optimized to rank #1 on Google for Georgian food in Prague.</p>
</td>
</tr>
</table>
<!-- Feature 2 -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;">
<tr>
<td width="56" style="vertical-align:top;padding:0 0 0 8px;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:48px;height:48px;background:linear-gradient(135deg,#ebf4ff,#bee3f8);border-radius:12px;text-align:center;vertical-align:middle;">
<span style="font-size:22px;line-height:48px;">&#128241;</span>
</td></tr>
</table>
</td>
<td style="padding:4px 0 16px 14px;border-bottom:1px solid #f0f0f0;">
<p style="font-size:15px;font-weight:700;color:#1a202c;margin:0;">Online Ordering & Reservations</p>
<p style="font-size:13px;color:#718096;line-height:1.6;margin:4px 0 0;">Customers can order khachapuri before they even walk in. Seamless booking, zero friction.</p>
</td>
</tr>
</table>
<!-- Feature 3 -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;">
<tr>
<td width="56" style="vertical-align:top;padding:0 0 0 8px;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:48px;height:48px;background:linear-gradient(135deg,#fefcbf,#faf089);border-radius:12px;text-align:center;vertical-align:middle;">
<span style="font-size:22px;line-height:48px;">&#128200;</span>
</td></tr>
</table>
</td>
<td style="padding:4px 0 16px 14px;border-bottom:1px solid #f0f0f0;">
<p style="font-size:15px;font-weight:700;color:#1a202c;margin:0;">SEO & Traffic Strategy</p>
<p style="font-size:13px;color:#718096;line-height:1.6;margin:4px 0 0;">Turn those 2,126 reviewers into repeat visitors and attract tourists searching for the best eats in Prague.</p>
</td>
</tr>
</table>
<!-- Feature 4 -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
<tr>
<td width="56" style="vertical-align:top;padding:0 0 0 8px;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:48px;height:48px;background:linear-gradient(135deg,#fed7e2,#feb2b2);border-radius:12px;text-align:center;vertical-align:middle;">
<span style="font-size:22px;line-height:48px;">&#128242;</span>
</td></tr>
</table>
</td>
<td style="padding:4px 0 16px 14px;">
<p style="font-size:15px;font-weight:700;color:#1a202c;margin:0;">Social Media Management</p>
<p style="font-size:13px;color:#718096;line-height:1.6;margin:4px 0 0;">Keep your 1,200+ Facebook followers engaged with fresh content, reels, and promotions that drive foot traffic.</p>
</td>
</tr>
</table>
</td></tr>

<!-- The Plan - 3 Steps -->
<tr><td style="padding:30px 45px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);border-radius:14px;overflow:hidden;">
<tr><td style="padding:32px 30px 10px;">
<p style="font-size:12px;font-weight:700;color:#e94560;letter-spacing:2px;text-transform:uppercase;margin:0;">Here's How We Start</p>
<h3 style="font-size:20px;font-weight:700;color:#ffffff;margin:8px 0 20px;">Three Simple Steps</h3>
</td></tr>
<tr><td style="padding:0 30px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
<tr>
<td width="40" style="vertical-align:top;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:34px;height:34px;background:linear-gradient(135deg,#e94560,#f5a623);border-radius:50%;text-align:center;">
<span style="font-size:15px;font-weight:800;color:#fff;line-height:34px;">1</span>
</td></tr>
</table>
</td>
<td style="padding:4px 0 0 10px;">
<p style="font-size:14px;font-weight:600;color:#ffffff;margin:0;">Free 30-Minute Strategy Call</p>
<p style="font-size:12px;color:#8899aa;margin:4px 0 0;line-height:1.5;">We map out your digital strategy together. No pressure, no commitment.</p>
</td>
</tr>
</table>
</td></tr>
<tr><td style="padding:0 30px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
<tr>
<td width="40" style="vertical-align:top;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:34px;height:34px;background:linear-gradient(135deg,#e94560,#f5a623);border-radius:50%;text-align:center;">
<span style="font-size:15px;font-weight:800;color:#fff;line-height:34px;">2</span>
</td></tr>
</table>
</td>
<td style="padding:4px 0 0 10px;">
<p style="font-size:14px;font-weight:600;color:#ffffff;margin:0;">Custom Website Mockup</p>
<p style="font-size:12px;color:#8899aa;margin:4px 0 0;line-height:1.5;">I'll design a mockup of your site &mdash; free. Just to show you what's possible.</p>
</td>
</tr>
</table>
</td></tr>
<tr><td style="padding:0 30px 28px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td width="40" style="vertical-align:top;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:34px;height:34px;background:linear-gradient(135deg,#e94560,#f5a623);border-radius:50%;text-align:center;">
<span style="font-size:15px;font-weight:800;color:#fff;line-height:34px;">3</span>
</td></tr>
</table>
</td>
<td style="padding:4px 0 0 10px;">
<p style="font-size:14px;font-weight:600;color:#ffffff;margin:0;">90-Day Growth Plan</p>
<p style="font-size:12px;color:#8899aa;margin:4px 0 0;line-height:1.5;">A clear roadmap to drive traffic and online orders within 90 days.</p>
</td>
</tr>
</table>
</td></tr>
</table>
</td></tr>

<!-- Closing text -->
<tr><td style="padding:30px 45px 0;">
<p style="font-size:15px;color:#4a5568;line-height:1.8;margin:0;">You've already done the hardest part &mdash; <strong>building a product people love</strong>. Let me help the internet catch up to your reputation.</p>
</td></tr>

<!-- CTA Buttons -->
<tr><td style="padding:30px 45px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr>
<td align="center" style="padding-bottom:12px;">
<!--[if mso]>
<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="mailto:mail@exzilcalanza.info?subject=Let's%20Talk%20-%20MATOKA%20Digital%20Strategy&body=Hi%20Exzil%2C%20I'd%20love%20to%20discuss%20the%20digital%20strategy%20for%20MATOKA." style="height:52px;v-text-anchor:middle;width:480px;" arcsize="50%" strokecolor="#e94560" fillcolor="#e94560">
<w:anchorlock/><center style="color:#ffffff;font-family:'Inter',sans-serif;font-size:16px;font-weight:700;">Reply to This Email</center>
</v:roundrect>
<![endif]-->
<!--[if !mso]><!-->
<a href="mailto:mail@exzilcalanza.info?subject=Let's%20Talk%20-%20MATOKA%20Digital%20Strategy&body=Hi%20Exzil%2C%20I'd%20love%20to%20discuss%20the%20digital%20strategy%20for%20MATOKA." style="display:block;background:linear-gradient(135deg,#e94560 0%,#c53030 100%);color:#ffffff;font-family:'Inter',sans-serif;font-size:16px;font-weight:700;text-decoration:none;padding:16px 40px;border-radius:50px;text-align:center;box-shadow:0 4px 15px rgba(233,69,96,0.4);">
&#9993;&#65039;&nbsp;&nbsp;Reply to This Email
</a>
<!--<![endif]-->
</td>
</tr>
<tr>
<td align="center" style="padding-bottom:8px;">
<a href="https://wa.me/639297932036?text=Hi%20Exzil%2C%20I'm%20interested%20in%20discussing%20digital%20growth%20for%20MATOKA.%20Let's%20talk!" style="display:block;background:linear-gradient(135deg,#25d366 0%,#128c7e 100%);color:#ffffff;font-family:'Inter',sans-serif;font-size:16px;font-weight:700;text-decoration:none;padding:16px 40px;border-radius:50px;text-align:center;box-shadow:0 4px 15px rgba(37,211,102,0.4);">
&#128172;&nbsp;&nbsp;Chat on WhatsApp
</a>
</td>
</tr>
</table>
</td></tr>

<!-- Divider -->
<tr><td style="padding:35px 45px 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td style="border-top:1px solid #e2e8f0;font-size:0;line-height:0;">&nbsp;</td></tr>
</table>
</td></tr>

<!-- Signature -->
<tr><td style="padding:25px 45px 35px;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr>
<td style="vertical-align:top;padding-right:18px;">
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td style="width:60px;height:60px;background:linear-gradient(135deg,#1a1a2e,#0f3460);border-radius:14px;text-align:center;vertical-align:middle;">
<span style="font-size:28px;font-weight:800;color:#ffffff;line-height:60px;font-family:'Inter',sans-serif;">EC</span>
</td></tr>
</table>
</td>
<td style="vertical-align:top;">
<p style="font-size:16px;font-weight:700;color:#1a202c;margin:0;">Exzil Calanza</p>
<p style="font-size:13px;color:#718096;margin:3px 0 0;">Digital Growth Strategist</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:10px;">
<tr>
<td style="padding-right:6px;"><span style="font-size:13px;">&#127760;</span></td>
<td><a href="https://exzilcalanza.info" style="font-size:13px;color:#e94560;text-decoration:none;font-weight:600;">exzilcalanza.info</a></td>
</tr>
</table>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:4px;">
<tr>
<td style="padding-right:6px;"><span style="font-size:13px;">&#128231;</span></td>
<td><a href="mailto:mail@exzilcalanza.info" style="font-size:13px;color:#4a5568;text-decoration:none;">mail@exzilcalanza.info</a></td>
</tr>
</table>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:4px;">
<tr>
<td style="padding-right:6px;"><span style="font-size:13px;">&#128241;</span></td>
<td><a href="https://wa.me/639297932036" style="font-size:13px;color:#4a5568;text-decoration:none;">+63 929 793 2036</a></td>
</tr>
</table>
</td>
</tr>
</table>
</td></tr>

</table>
</td></tr>

<!-- ======== FOOTER ======== -->
<tr><td>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f7fafc;border-radius:0 0 16px 16px;border-top:1px solid #e2e8f0;">
<tr><td style="padding:25px 45px;text-align:center;">
<p style="font-size:11px;color:#a0aec0;line-height:1.6;margin:0;">This email was sent by Exzil Calanza. You received this because your business was identified as a potential fit for digital growth services.</p>
<p style="font-size:11px;color:#a0aec0;margin:8px 0 0;">If you no longer wish to receive emails, simply reply with "unsubscribe" and we'll remove you immediately.</p>
</td></tr>
</table>
</td></tr>

</table>
<!-- End main container -->

</td></tr>
</table>
<!-- End full-width wrapper -->

</body>
</html>"""

text_body = """Hi MATOKA team,

I recently came across your bakery while exploring the best Georgian food spots in Prague — and your reputation is incredible. 2,126 Google reviews, a 4.7-star rating, and you've earned the title of "Khachapuri Kings in Prague."

But here's the thing: when someone Googles "MATOKA Georgian Prague" right now, there's no website to land on. No menu to browse, no online ordering, no story about your Georgian roots. You're leaving customers and revenue on the table every single day.

WHAT I'LL BUILD FOR YOU:

🌐 Stunning Website — showcasing your menu, story, and Vinohrady location, optimized to rank #1 on Google
📱 Online Ordering & Reservations — customers can order khachapuri before they walk in
📈 SEO & Traffic Strategy — turn those 2,126 reviewers into repeat visitors
📲 Social Media Management — keep followers engaged with fresh content and promotions

HERE'S HOW WE START:

1. Free 30-Minute Strategy Call — no pressure, no commitment
2. Custom Website Mockup — free, just to show you what's possible
3. 90-Day Growth Plan — a clear roadmap to drive traffic and online orders

You've already done the hardest part — building a product people love. Let me help the internet catch up to your reputation.

Reply to this email or chat on WhatsApp: https://wa.me/639297932036

Best regards,
Exzil Calanza
🌐 exzilcalanza.info
📧 mail@exzilcalanza.info
📱 +63 929 793 2036
"""

response = ses.send_email(
    FromEmailAddress='Exzil Calanza <mail@exzilcalanza.info>',
    Destination={
        'ToAddresses': ['mail@exzilcalanza.info']
    },
    Content={
        'Simple': {
            'Subject': {
                'Data': 'MATOKA — Your Bakery Deserves a Digital Empire 🚀',
                'Charset': 'UTF-8'
            },
            'Body': {
                'Text': {
                    'Data': text_body,
                    'Charset': 'UTF-8'
                },
                'Html': {
                    'Data': html_body,
                    'Charset': 'UTF-8'
                }
            }
        }
    },
    ConfigurationSetName='my-first-configuration-set'
)

print("Email sent!")
print("Message ID:", response['MessageId'])
