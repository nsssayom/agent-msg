"""Optional rendered UI check: requires Playwright only in the test environment."""
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from agent_msg.journal import Journal
from agent_msg.web import make_server


def main():
    from playwright.sync_api import sync_playwright, expect
    root=Path(__file__).resolve().parents[1]
    output=root/'test/results';output.mkdir(exist_ok=True)
    rows=json.loads((root/'test/fixtures/messages.json').read_text())
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'journal.db'
        with Journal(path) as journal:
            for row in rows:
                envelope={k:v for k,v in row.items() if k not in ('seq','status','observed','deliveries')}
                journal.append(envelope,row['observed'])
                for event in row['deliveries']:
                    journal.event(row['id'],event['status'],event['transport'],event['detail'])
        server,url=make_server(path)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch()
                page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url);page.wait_for_selector('.row')
                assert page.locator('.row').count()==10
                assert 'token' not in page.url
                page.locator('.row').filter(has_text='Can you review the parser change?').click()
                page.wait_for_selector('.message-card')
                assert page.locator('.message-card').count()==3
                page.screenshot(path=str(output/'journal-desktop.png'),full_page=True)
                page.locator('#q').fill('Literal content')
                expect(page.locator('.row')).to_have_count(1)
                page.locator('.row').click()
                expect(page.locator('.message-body')).to_have_count(1)
                expect(page.locator('.message-body')).to_contain_text('<script>')
                assert page.evaluate('window.pwned') is None
                page.locator('#q').fill('no-matching-messages')
                expect(page.locator('.row')).to_have_count(0)
                assert page.locator('#list-note').is_visible()
                page.locator('#q').fill('')
                expect(page.locator('.row')).to_have_count(10)
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(output/'journal-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile overflow'
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(2)
    print('UI smoke passed: conversation, search, empty state, inert HTML, desktop/mobile layout.')


if __name__=='__main__':main()
