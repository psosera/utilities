#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# ///

from csv import DictReader
from dataclasses import dataclass
import time
import sys

@dataclass
class Advisee:
    name: str
    email: str
    id: str

def make_advising_url(advisee: Advisee) -> str:
    return f'https://colss-prod.ec.grinnell.edu/Student/Planning/Advisors/Advise/{advisee.id}'

def make_advisee_row(advisee: Advisee) -> str:
    return f'''
        <tr>
            <td>{advisee.name}</td>
            <td><a href="mailto:{advisee.email}">{advisee.email}</a></td>
            <td><a href="{make_advising_url(advisee)}" target="_blank">{advisee.id}</a></td>
        </tr>
    '''

def make_advisee_table(advisees: list[Advisee]) -> str:
    rows = '\n'.join(make_advisee_row(advisee) for advisee in advisees)
    return f'''
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Advisee ID</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
    '''

def make_mail_all_link(advisees: list[Advisee]) -> str:
    emails = ','.join(advisee.email for advisee in advisees)
    return f'<a href="mailto:{emails}">Email All Advisees</a>'

def make_body(advisees: list[Advisee]) -> str:
    return f'''
        <h1>Advising Page {time.strftime("%Y-%m-%d")}</h1>
        <p>{make_mail_all_link(advisees)}</p>
        {make_advisee_table(advisees)}
    '''

def print_webpage(title: str, body: str):
    print(f'''
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{title}</title>
            <link
                rel="stylesheet"
                href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css"
            >
        </head>
        <body>
            {body}
        </body>
    </html>   
    ''')

def main(args: list[str]):
    if len(args) != 2:
        print(f'Usage: {args[0]} <csvfile>')
        exit(1)
    csvfile = args[1]
    advisees: list[Advisee] = []
    with open(csvfile, newline='', encoding='utf-8-sig') as f:
        reader = DictReader(f, dialect='excel')
        for row in reader:
            advisees.append(Advisee(row['Name'], row['Advisee Preferred Email'], row['ID']))
    print_webpage(f'Advising Page {time.strftime("%Y-%m-%d")}', make_body(advisees))

if __name__ == '__main__':
    main(sys.argv)
