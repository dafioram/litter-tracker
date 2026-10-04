import re
from datetime import datetime, timedelta


def recent_csv():
    """Two complete cycles (one interrupted) from yesterday, newest first."""
    d = datetime.now() - timedelta(days=1)
    md = f"{d.month}/{d.day}"
    csv = f"""Activity,Timestamp,Value
Clean Cycle Complete,{md} 7:01 am,-
Cycle interrupted,{md} 6:58 am,-
Clean Cycle In Progress,{md} 6:58 am,-
Weight recorded,{md} 6:51 am,9.6 lbs
Cat detected,{md} 6:43 am,-
Clean Cycle Complete,{md} 6:10 am,-
Clean Cycle In Progress,{md} 6:07 am,-
Weight recorded,{md} 6:00 am,9.5 lbs
Cat detected,{md} 5:58 am,-
"""
    return csv, f"litter-robot_4_activity_{datetime.now():%Y-%m-%d}.csv"


def robot_stat(html, label):
    return re.search(r'>\s*([\d.]+)\s*</div>\s*<div[^>]*>' + label + '<', html).group(1)


def test_cycle_count_counts_each_cycle_once(client, upload):
    upload(*recent_csv())
    html = client.get('/').get_data(as_text=True)
    assert robot_stat(html, 'Cycles') == '2'
    assert robot_stat(html, 'Interrupts') == '1'
