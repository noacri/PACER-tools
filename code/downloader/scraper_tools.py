import json
import time
import os
import re
import sys
from hashlib import md5
from pathlib import Path
from datetime import datetime, timedelta

import pytz
import pandas as pd
from pandas import to_datetime
from selenium.webdriver import FirefoxOptions

sys.path.append(str(Path(__file__).resolve().parents[1]))
from downloader import forms
from support import settings
from support import data_tools as dtools
from support import fhandle_tools as ftools

# Default runtime hours
PACER_HOURS_START = 20
PACER_HOURS_END = 4

# Misc re
re_no_docket = r'(There are )?(P|p)roceedings for case .{1,50} (but none satisfy the selection criteria|are not available)'
re_members_block = r"Member cases: <table [\s\S]+?</table>"

def month_chunker(date_from, date_to, chunk_size=31):
    ''' Break a time period into chunks '''

    date_from = pd.to_datetime(date_from)
    date_to = pd.to_datetime(date_to)
    if date_to > date_to:
        raise ValueError('date_to cannot be before date_from')

    month = timedelta(days=chunk_size)
    left_date = date_from
    right_date = left_date + month
    chunks = []
    while right_date < date_to:
        chunks.append((left_date, right_date))
        left_date += month
        right_date += month
    # Add the final chunk (less than a full period)
    chunks.append( (left_date, date_to))
    return chunks

def split_config(config, date_pairs, max_gap=180, chunk_size=31):
    '''
    Takes configs and splits into list of configs
    Inputs:
        - config (dict): a single query config file
        - date_pairs (list): a list of pairs of keys e.g. [('date_from'), ('date_to')]
        - mag_gap (int): the largest allowed date range, above this value gets chunked
        - chunk_size (int): the size of chunks
    Output:
        A list of config files with the dates chunked
    '''
    config_list = []

    # Check if date range is too wide
    for date_from_field, date_to_field in date_pairs:
        if config.get(date_from_field):
            date_from = pd.to_datetime(config.get(date_from_field))
            date_to = pd.to_datetime(config.get(date_to_field)) or datetime.today()

            if abs(date_from - date_to).days >= max_gap:
                date_ranges = month_chunker(date_from, date_to, chunk_size)
                for left, right in date_ranges:
                    new_config = {**config}
                    new_config[date_from_field] = left.strftime('%m/%d/%Y')
                    new_config[date_to_field] = right.strftime('%m/%d/%Y')
                    config_list.append(new_config)
                return config_list

    #Else return singleton list with original config
    return [config]

def login(browser, auth, login_url=None, logging=None):
    '''
    Method to log in to a court
    Inputs:
        - browser: browser instance
        - auth_path (str or Path): path to login details
        - login_url (str): url to login page, if empty assumes browser is already there
        - logging: logging instance
    '''
    if login_url:
        browser.get(login_url)

    # Check if already logged in
    if is_logged_in(browser):
        return True

    fill_values =  {'username':auth['user'], 'password':auth['pass']}
    login_form = forms.FormFiller(browser, 'login',fill_values)
    login_form.fill()
    time.sleep(1)
    login_form.submit()
    time.sleep(2)

    # Check if details correct
    if "Invalid username or password" in browser.page_source:
        logging.error('Invalid Username or Password')
        return False
    else:
        if logging:
            logging.info('Login succesful')
        return True

def parse_goDLS_string(string):
    '''
    Take a goDLS string and return a dictionary of arguments
    Inputs:
        - string: a goDLS string like "goDLS('k','g','b','e','a','c','i','l')"
    Output:
        A dictionary or arguments e.g.
        {'action': '/doc1/02311840388', 'caseid': '56434', ...etc }
    '''
    re_args = rf''',\s?'''.join([rf"'(?P<{arg}>.*?)'" for arg in GODLS['args']])
    re_full = f"goDLS\({re_args}\)"
    match = re.search(re_full, string)
    if match:
        result = match.groupdict()
        # Separate out the doc_id
        result['action_doc_id'] = result['action'].split('/')[-1]
        return result

def build_goDLS(arg_dict, **kwargs):
    ''' Generate a goDLS js string from an argument dictionary'''
    final_args = {**arg_dict, **kwargs}
    arg_string = ",".join([f"'{final_args[k]}'" for k in GODLS['args'] ])
    return f"goDLS({arg_string})"

def get_link_attrs(el, mode, get_godls=False):
    '''
    From a bs4 tag get the href string and goDLS dictionary
    Inputs:
        - el (WebElement)
        - mode ('bs4' or 'selenium')
        - get_godls (bool): whether or not to get the godls data
    '''
    if mode == 'bs4':
        href =  el.attrs.get('href')
        onclick = el.attrs.get('onclick')
    elif mode == 'selenium':
        href =  el.get_attribute('href')
        onclick = el.get_attribute('onclick')

    if get_godls:
        try:
            go_dls_string = onclick.split(';')[0]
            go_dls_dict = parse_goDLS_string(go_dls_string) or {}
        except:
            go_dls_dict = {}
        return {'href': href, 'go_dls':go_dls_dict}

    return {'href': href}

def get_single_link(el, mode='bs4'):
    '''
    Get the doc no, link and goDLS data for a single document (bs4 a-tag)

    Inputs:
        - el (WebElement): the WebElement for the document link a-tag
        - mode ('bs4' or 'selenium')
    Output:
        dict
    '''
    doc = {}
    doc['ind'] = el.text
    doc.update(get_link_attrs(el,mode))
    return doc

def get_document_links(docket_table, get_att=False, filter_fn=None, wanted_doc_nos={} ):
    '''
    Get links to documents from the docket_table
    Inputs:
        - docket_table (WebElement): the docket report main table
        - get_att (bool): if false skips the document attachments
        - filter_fn (function): filter applied to docket text first n characters
            e.g. lambda x: bool(re.search(r"^NOTICE",x,re.I)))
        - wanted_doc_nos (dict): dict of specific doc nos to get with (line_no, attachment) pairs
    Output:
        List of dicts
    '''

    # Get all doc ids from line doc links (second column)
    line_doc_ids = [ x.attrs.get('href').split('/')[-1]
                     for x in docket_table.select('tr td:nth-of-type(2) a') ]

    docs = []
    for row in docket_table.select('tr')[1:]:
        # The number in the second column of table
        td_line = row.select_one('td:nth-of-type(2) a')
        if not td_line:
            continue
        else:
            td_line_no = td_line.text.strip()

        # Decide flow
        get_line_doc = (not bool(wanted_doc_nos)) or ('0' in wanted_doc_nos.get(td_line_no,[]) )

        get_line_atts = (not bool(wanted_doc_nos) and get_att) \
                            or len([x for x in wanted_doc_nos.get(td_line_no,[])if x!='0'])>0

        # # Skip if wanted_doc_nos specified but line_no not one of the wanted lines
        # if wanted_doc_nos and td_line_no not in wanted_doc_nos.keys():
        #     continue

        if not (get_line_doc or get_line_atts):
            continue

        td_docket_text = row.select('td')[2]

        # Get the line doc info if no wanted_doc_nos specified or if '0' in list
        if get_line_doc:
            doc = get_single_link(td_line)
            doc['get_line_doc'] = True
            #
            #
            # docket_text = td_docket_text.text[:200]
            # if filter_fn != None:
            #     if not filter_fn(docket_text):
            #         continue
            # doc['text'] = docket_text


        else:
            doc = {'ind': td_line_no}
            doc['get_line_doc'] = False

        # Only pulls documents that are case_related, not external urls
        atts = []
        if get_line_atts:
            # Isolate the atags of interest
            atags = td_docket_text.select('a')
            # Filter out external links (by only using digit links)
            atags = [a for a in atags if a.text.strip().isdigit()]
            # Filter out references to previous lines
            atags = [a for a in atags if a.attrs.get('href').split('/')[-1] not in line_doc_ids]

            # Filter down by wanted_doc_nos
            if wanted_doc_nos:
                atags = [a for a in atags if a.text.strip() in wanted_doc_nos.get(td_line_no,[]) ]

            atts = [get_single_link(a) for a in atags]

        doc['atts'] = atts
        docs.append(doc)

    return docs

def get_recent_download(dir, ext=None, wait_time=2, time_buffer=30, retry_count=0):
    '''
    Get the most recently downloaded file in the given directory
    Inputs:
        - dir (str or Path): the directory
        - ext (str): the file extension
        - wait_time (int): the amount of time to wait for the download
        - time_buffer (int): the number of seconds buffer to add to wait_time
        - retry_count (int): no. of times its been retried
    Output:
        Path
    '''
    retry_limit = 1

    time.sleep(wait_time)
    pattern = "*.{ext}" if ext else "*"
    files = list(x for x in Path(dir).glob(pattern) if x.is_file() )
    if not len(files):
        return

    # Get most recent file base on created time
    candidate = max(files, key=lambda x: x.lstat().st_ctime)
    if (time.time() - candidate.lstat().st_ctime) < (wait_time + time_buffer):

        # Check that it's not a 0Kb file
        if candidate.lstat().st_size > 0:
            return candidate
        elif retry_count < retry_limit:
            rc = retry_count + 1
            return get_recent_download(dir, ext, wait_time, time_buffer, retry_count=rc)


def get_pacer_url(court, page):
    '''
    Get a court-specific pacer url
    Inputs:
        - court (str): court abbrev. e.g. ilnd
        - page (str): the pacer page you want a url for
    Ouput:
        url (str)
    '''
    if court=='psc':
        base_url = "https://dcecf.psc.uscourts.gov/"
    else:
        base_url = f"https://ecf.{court}.uscourts.gov/"

    if page == 'query':
        return base_url + 'cgi-bin/iquery.pl'
    elif page == 'login':
        return base_url + 'cgi-bin/login.pl'
    elif page == 'logout':
        return base_url + 'cgi-bin/login.pl?logout'
    elif page == 'docket':
        return base_url + "cgi-bin/DktRpt.pl"
    elif page == 'doc_link':
        return base_url + 'doc1'
    elif page == 'possible_case':
        return base_url + 'cgi-bin/possible_case_numbers.pl'

def remap_date_year_backwards_to_forwards(xdate):
    '''
    Turns a date of type MM/DD/YY to YYYY-MM-DD
    '''
    m, d, y = xdate.strip().split('/')
    return '-'.join(['20'+y, m, d])

def extract_query_filedate(query_date_str):
    '''
    Extracts the filing date from the query date string of type "filed XX/XX/XX closed XX/XX/XX"
    and reworks it to match XXXX-XX-XX
    '''
    if 'closed' in query_date_str:
        filedate = query_date_str.lstrip('filed ').split(' closed')[0]
    else:
        filedate = query_date_str.lstrip('filed ').strip()
    return remap_date_year_backwards_to_forwards(filedate)

def extract_query_termdate(query_date_str):
    '''Extracts the termination date from the query date string of type "filed XX/XX/XX closed XX/XX/XX"'''
    if 'closed' in query_date_str:
        termdate = query_date_str.split(' closed ')[-1]
        return remap_date_year_backwards_to_forwards(termdate)
    else:
        return None

def is_logged_in(browser):
    ''' Check if logged in to pacer, returns true if logout is in the navbar'''

    # Check if the navbar is there
    nav = browser.find_elements_by_css_selector('#topmenu')
    if not len(nav):
        return None
    # Get all the links
    nav_links = nav[0].find_elements_by_css_selector('a')
    # Clean the link text and return true if logout is in
    return 'logout' in [link.text.lower().replace(' ','').strip() for link in nav_links]

def get_firefox_options(download_dir):
    '''Default options for firefox'''
    options = FirefoxOptions()
    options.set_preference("browser.download.folderList", 2)
    options.set_preference("browser.download.dir", str(download_dir))
    options.set_preference("browser.helperApps.neverAsk.saveToDisk", "application/pdf")
    options.set_preference("plugin.disable_full_page_plugin_for_types", "application/pdf")
    options.set_preference("pdfjs.disabled", True)
    return options

def get_time_central(as_string=False):
    '''Get the current time in central time zone'''
    utc = pytz.timezone('UTC')
    now = utc.localize(datetime.utcnow())

    central = pytz.timezone('US/Central')
    res = now.astimezone(central)
    return res if not as_string else res.strftime(ftools.FMT_TIME)

def check_time_continue(start, end, current_ind, logging=None):
    ''' Check if scraper should continue'''
    no_run_hours = range(start-1,end-1,-1)
    if get_time_central().hour in no_run_hours:
        print(f'Scraping operating outside of running hours, terminating (at file index {current_ind})')
        if logging:
            logging.info(f'Scraping operating outside of running hours, terminating (at file index {current_ind})')
        return False
    else:
         return True

def clean_temp_download_folders(court_dir, user_hash=None):
    '''
    Cleans up all temporary download folders in a court directory

    Inputs:
        - court_dir (PacerCourtDir object): the directory for the court
        - user_hash (hash): the user hash

    '''
    if not user_hash:
        user_hash = ftools.gen_user_hash('renamed_by_script')

    for subdir in (x for x in court_dir._temp_.glob('*') if x.is_dir()):

        for file in (x for x in subdir.glob('*') if x.is_file()):
            # Delete zero sized files
            if file.stat().st_size == 0:
                file.unlink()

            else:
                try:
                    # Rename based on the pdf header
                    doc_id = ftools.get_correct_document_id(file, court_dir.court)
                    fname = ftools.generate_document_fname(doc_id, user_hash)
                    file.replace(court_dir.docs/fname)
                except:
                    print(f"Cannot rename {file}")

def get_member_list_span(case_string):
    ''' Get the beginning and end positions of member cases

    Inputs:
        - case_string (str): the string of the case html file
    Outputs:
        beg, end (int): the beggining and end positions of the member list block
    '''
    match = re.search(re_members_block, case_string, re.I)
    if match:
        return match.span()
    else:
        return None,None