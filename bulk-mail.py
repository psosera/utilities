#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///

from csv import DictReader
import urllib.parse
import sys

db_row  = dict[str, str]
db_type = dict[str, db_row]

def parse_template(template_file: str) -> tuple[str, str]:
    with open(template_file, encoding='utf-8-sig') as f:
        lines = f.readlines()
        return (lines[0].strip(), ''.join(lines[1:]))

def load_db(csvfile: str) -> db_type:
    with open(csvfile, newline='', encoding='utf-8-sig') as f:
        reader = DictReader(f, dialect='excel')
        ret: db_type = { }
        for row in reader:
            keys = row.keys()
            if 'email' not in keys:
                raise RuntimeError('row does not include "email" field')
            ret[row['email']] = { k : row[k] for k in keys if k != 'email' }
        return ret

def instantiate_template(template: str, email: str, row: db_row):
    for key in row.keys():
        template = template.replace(f'{{{{{key}}}}}', row[key])
    return template.replace('{{email}}', email)

def make_email_link(subject: str, body: str, email: str, row: db_row):
    subject = urllib.parse.quote(instantiate_template(subject, email, row), safe='', encoding=None, errors=None)
    body = urllib.parse.quote(instantiate_template(body, email, row), safe='', encoding=None, errors=None)
    return f'<a href="mailto:{email}?subject={subject}&body={body}">{email}</a>'

def print_page(db: db_type, subject: str, body: str):
    print(f'''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Email Page</title>
</head>
<body>
  <ul>
'''.strip())
    for email in db.keys():
        print(f'    <li>{make_email_link(subject, body, email, db[email])}</li>')
    print('''
  </ul>
</body>
</html>
'''.strip())

def print_help(prog: str):
    print(f'Usage: {prog} <template> <csvfile>')

def main(args: list[str]):
    if len(args) != 3:
        print_help(args[0])
        exit(1)
    subject, body = parse_template(args[1])
    db = load_db(args[2])
    print_page(db, subject, body)

if __name__ == '__main__':
    main(sys.argv)
