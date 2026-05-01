"""
Import miner inventory from spreadsheet into miner-testbench postgres DB.

Strategy:
- Match on serial_number (box or miner barcode) or mac_address
- If record exists: add sheet data as historical context only — never overwrite
  existing ip/mac/status/location fields that came from live testing
- If record doesn't exist: insert with available fields, status='unknown',
  ip_address='0.0.0.0' (placeholder), mark as inventory_only=True
- All inventory data goes into new columns: client_name, box_serial, miner_serial,
  psu_serial, nameplate_hashrate, inventory_location, inventory_subunit,
  inventory_source (tab name), inventory_notes, historical_names (JSON array)
"""

import psycopg2
import json
import openpyxl
from datetime import datetime

XLSX = '/tmp/miners.xlsx'
DB_HOST = 'localhost'
DB_PORT = 5460
DB_NAME = 'testbench'
DB_USER = 'testbench'
DB_PASS = 'testbench'

def normalize_serial(s):
    if not s:
        return None
    s = str(s).strip().upper()
    if not s or s in ('NONE', 'N/A', ''):
        return None
    return s

def normalize_mac(s):
    if not s:
        return None
    s = str(s).strip().lower()
    if len(s) < 10:
        return None
    return s

def clean(val):
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None

def parse_sheet(wb):
    """Parse all sheets into a list of miner inventory dicts."""
    miners = []
    
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if sheet_name in ('scratch', 'Box scanning '):
            continue
            
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        
        # Special handling for BlockMetrix shipping (2-col format)
        if sheet_name == 'BlockMetrix shipping':
            for row in rows[1:]:
                if not any(row):
                    continue
                miner_id = clean(row[0]) if len(row) > 0 else None
                serial = normalize_serial(row[1]) if len(row) > 1 else None
                if serial:
                    miners.append({
                        'source_tab': sheet_name,
                        'miner_id': miner_id,
                        'box_serial': serial,
                        'miner_serial': None,
                        'psu_serial': None,
                        'model': None,
                        'nameplate_hashrate': None,
                        'mac': None,
                        'location': None,
                        'subunit': None,
                        'notes': None,
                        'client_name': 'BlockMetrix',
                    })
            continue
        
        # Special handling for texas mid 2 (different column order)
        if sheet_name == 'texas mid 2':
            # Cols: Miner ID, (blank), Barcode Location, Scanned code, Miner type, Last 4 Mac, Location, subunit...
            current_miner = {}
            for row in rows[1:]:
                if not any(row):
                    continue
                miner_id = clean(row[0]) if len(row) > 0 else None
                barcode_loc = clean(row[2]) if len(row) > 2 else None
                scanned = normalize_serial(row[3]) if len(row) > 3 else None
                model = clean(row[4]) if len(row) > 4 else None
                location = clean(row[6]) if len(row) > 6 else None
                subunit = clean(row[7]) if len(row) > 7 else None
                comments = clean(row[9]) if len(row) > 9 else None
                
                if miner_id:
                    if current_miner.get('box_serial') or current_miner.get('miner_serial'):
                        miners.append(current_miner)
                    client = miner_id.split('-')[0] if miner_id and '-' in miner_id else miner_id
                    current_miner = {
                        'source_tab': sheet_name,
                        'miner_id': miner_id,
                        'box_serial': None,
                        'miner_serial': None,
                        'psu_serial': None,
                        'model': model,
                        'nameplate_hashrate': None,
                        'mac': None,
                        'location': location,
                        'subunit': str(subunit) if subunit else None,
                        'notes': comments,
                        'client_name': client,
                    }
                
                if barcode_loc == 'Box' and scanned:
                    current_miner['box_serial'] = scanned
                    if model: current_miner['model'] = model
                    if location: current_miner['location'] = location
                elif barcode_loc == 'Miner' and scanned:
                    current_miner['miner_serial'] = scanned
                elif barcode_loc == 'PSU' and scanned:
                    current_miner['psu_serial'] = scanned
                    
            if current_miner.get('box_serial') or current_miner.get('miner_serial'):
                miners.append(current_miner)
            continue
        
        # Standard format sheets
        # Header: Miner ID, Barcode Location, Scanned code, Miner type, Nameplate hash rate, MAC, Operating Location, subunit, count, Comments
        current_miner = {}
        for row in rows[1:]:
            if not any(row):
                continue
            
            miner_id = clean(row[0]) if len(row) > 0 else None
            barcode_loc = clean(row[1]) if len(row) > 1 else None
            scanned = normalize_serial(row[2]) if len(row) > 2 else None
            model = clean(row[3]) if len(row) > 3 else None
            hashrate = row[4] if len(row) > 4 else None
            mac = normalize_mac(row[5]) if len(row) > 5 else None
            location = clean(row[6]) if len(row) > 6 else None
            subunit = clean(row[7]) if len(row) > 7 else None
            comments = clean(row[9]) if len(row) > 9 else None
            
            try:
                hashrate = float(hashrate) if hashrate and str(hashrate).replace('.','').isdigit() else None
            except:
                hashrate = None
            
            if miner_id:
                if current_miner.get('box_serial') or current_miner.get('miner_serial'):
                    miners.append(current_miner)
                client = miner_id.split('-')[0] if miner_id and '-' in miner_id else miner_id
                current_miner = {
                    'source_tab': sheet_name,
                    'miner_id': miner_id,
                    'box_serial': None,
                    'miner_serial': None,
                    'psu_serial': None,
                    'model': model,
                    'nameplate_hashrate': hashrate,
                    'mac': mac,
                    'location': location,
                    'subunit': str(subunit) if subunit else None,
                    'notes': comments,
                    'client_name': client,
                }
            
            if not current_miner:
                continue
            
            if barcode_loc == 'Box' and scanned:
                current_miner['box_serial'] = scanned
                if model and not current_miner.get('model'): current_miner['model'] = model
                if mac and not current_miner.get('mac'): current_miner['mac'] = mac
                if location and not current_miner.get('location'): current_miner['location'] = location
            elif barcode_loc == 'Miner' and scanned:
                current_miner['miner_serial'] = scanned
                if comments and current_miner.get('notes'):
                    current_miner['notes'] += ' | ' + comments
                elif comments:
                    current_miner['notes'] = comments
            elif barcode_loc == 'PSU' and scanned:
                current_miner['psu_serial'] = scanned
                if comments and current_miner.get('notes'):
                    current_miner['notes'] += ' | ' + comments
                elif comments:
                    current_miner['notes'] = comments
            elif barcode_loc == 'control' and scanned:
                current_miner['box_serial'] = scanned
        
        if current_miner.get('box_serial') or current_miner.get('miner_serial'):
            miners.append(current_miner)
    
    return miners


def run():
    wb = openpyxl.load_workbook(XLSX)
    miners = parse_sheet(wb)
    print(f"Parsed {len(miners)} miner records from spreadsheet")
    
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    cur = conn.cursor()
    
    # Add inventory columns if they don't exist
    new_cols = [
        ('client_name', 'VARCHAR(128)'),
        ('box_serial', 'VARCHAR(64)'),
        ('psu_serial', 'VARCHAR(64)'),
        ('nameplate_hashrate', 'DOUBLE PRECISION'),
        ('inventory_location', 'VARCHAR(256)'),
        ('inventory_subunit', 'VARCHAR(64)'),
        ('inventory_source', 'VARCHAR(64)'),
        ('inventory_notes', 'TEXT'),
        ('inventory_only', 'BOOLEAN DEFAULT FALSE'),
        ('historical_names', 'JSON'),
        ('inventory_imported_at', 'TIMESTAMP'),
    ]
    
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='miners'")
    existing_cols = {row[0] for row in cur.fetchall()}
    
    for col_name, col_type in new_cols:
        if col_name not in existing_cols:
            print(f"  Adding column: {col_name}")
            cur.execute(f"ALTER TABLE miners ADD COLUMN {col_name} {col_type}")
    
    conn.commit()
    print("Schema migration done")
    
    inserted = 0
    updated = 0
    skipped = 0
    
    for m in miners:
        box_s = m.get('box_serial')
        miner_s = m.get('miner_serial')
        mac = m.get('mac')
        miner_id_label = m.get('miner_id')
        
        # Try to find existing record
        existing = None
        matched_by = None
        
        # Match priority: miner_serial > box_serial > mac
        for serial, label in [(miner_s, 'serial_number'), (box_s, 'box_serial')]:
            if serial:
                cur.execute(f"SELECT id, serial_number, mac_address, historical_names, client_name FROM miners WHERE serial_number = %s OR box_serial = %s", (serial, serial))
                row = cur.fetchone()
                if row:
                    existing = row
                    matched_by = label
                    break
        
        if not existing and mac:
            cur.execute("SELECT id, serial_number, mac_address, historical_names, client_name FROM miners WHERE mac_address = %s", (mac,))
            row = cur.fetchone()
            if row:
                existing = row
                matched_by = 'mac_address'
        
        now = datetime.utcnow()
        
        if existing:
            # Update only inventory fields that aren't already set; preserve live data
            rec_id = existing[0]
            hist_names = existing[3] or []
            if isinstance(hist_names, str):
                try: hist_names = json.loads(hist_names)
                except: hist_names = []
            
            # Add miner_id label as historical name if new
            if miner_id_label and miner_id_label not in hist_names:
                hist_names.append(miner_id_label)
            
            cur.execute("""
                UPDATE miners SET
                    client_name = COALESCE(client_name, %s),
                    box_serial = COALESCE(box_serial, %s),
                    psu_serial = COALESCE(psu_serial, %s),
                    nameplate_hashrate = COALESCE(nameplate_hashrate, %s),
                    inventory_location = COALESCE(inventory_location, %s),
                    inventory_subunit = COALESCE(inventory_subunit, %s),
                    inventory_source = COALESCE(inventory_source, %s),
                    inventory_notes = COALESCE(inventory_notes, %s),
                    historical_names = %s,
                    inventory_imported_at = %s
                WHERE id = %s
            """, (
                m.get('client_name'),
                box_s,
                m.get('psu_serial'),
                m.get('nameplate_hashrate'),
                m.get('location'),
                m.get('subunit'),
                m.get('source_tab'),
                m.get('notes'),
                json.dumps(hist_names),
                now,
                rec_id
            ))
            updated += 1
        else:
            # Insert new inventory-only record
            # Use miner_serial as serial_number (primary); box as box_serial
            serial_number = miner_s or box_s
            
            # historical_names starts with the miner_id label
            hist_names = [miner_id_label] if miner_id_label else []
            
            try:
                cur.execute("""
                    INSERT INTO miners (
                        ip_address, mac_address, serial_number, model, miner_type,
                        psu_fault, control_board_fault, dead_chips,
                        ai_auto_fixable, status, first_seen, last_seen,
                        client_name, box_serial, psu_serial, nameplate_hashrate,
                        inventory_location, inventory_subunit, inventory_source,
                        inventory_notes, inventory_only, historical_names, inventory_imported_at
                    ) VALUES (
                        '0.0.0.0', %(mac)s, %(serial)s, %(model)s, 'antminer',
                        FALSE, FALSE, 0,
                        FALSE, 'unknown', %(now1)s, %(now2)s,
                        %(client)s, %(box)s, %(psu)s, %(nhash)s,
                        %(loc)s, %(sub)s, %(src)s,
                        %(notes)s, TRUE, %(hist)s, %(now3)s
                    )
                    ON CONFLICT (mac_address) DO NOTHING
                """, {
                    'mac': mac, 'serial': serial_number, 'model': m.get('model'),
                    'now1': now, 'now2': now,
                    'client': m.get('client_name'), 'box': box_s, 'psu': m.get('psu_serial'),
                    'nhash': m.get('nameplate_hashrate'),
                    'loc': m.get('location'), 'sub': m.get('subunit'), 'src': m.get('source_tab'),
                    'notes': m.get('notes'), 'hist': json.dumps(hist_names), 'now3': now
                })
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"  SKIP {serial_number}: {e}")
                conn.rollback()
                skipped += 1
                continue
        
        conn.commit()
    
    print(f"\nDone: {inserted} inserted, {updated} updated, {skipped} skipped")
    
    # Summary
    cur.execute("SELECT COUNT(*) FROM miners")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM miners WHERE inventory_only = TRUE")
    inv_only = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM miners WHERE inventory_only = FALSE OR inventory_only IS NULL")
    live = cur.fetchone()[0]
    print(f"Total miners in DB: {total} ({live} live-tested, {inv_only} inventory-only)")
    
    cur.close()
    conn.close()

if __name__ == '__main__':
    run()
