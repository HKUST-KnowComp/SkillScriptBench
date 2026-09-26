"""Separate option references from executable invocations without dropping either."""
from invocation_inventory_v4 import inventory as original_inventory
from invocation_inventory_v4 import neutral_request, python_interfaces


def inventory(text):
    rows = original_inventory(text)
    for row in rows:
        if row['kind'] == 'inline_argument':
            row['kind'] = 'option_reference'
    return rows
