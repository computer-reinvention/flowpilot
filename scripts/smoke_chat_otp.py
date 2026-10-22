"""Real Pi chooses chat input on a synthetic OTP page; no routing hints in task."""
import asyncio
import tempfile

from dotenv import load_dotenv

from backend.session import BrowserSession
from scripts.smoke_handoff import HTML


async def main():
    load_dotenv()
    questions = []
    handoffs = []

    async def answer(question):
        questions.append(question)
        assert question['kind'] == 'inputs', question
        return {'values': {field['name']: '246810' for field in question['fields']}}

    async def unexpected_handoff(event):
        handoffs.append(event)
        raise RuntimeError('Unexpected browser takeover for a text code')

    with tempfile.TemporaryDirectory() as state:
        async with BrowserSession('chat-otp', state, on_question=answer,
                                  on_handoff=unexpected_handoff) as session:
            await session.context.route('https://handoff.test/**',
                                        lambda route: route.fulfill(body=HTML, content_type='text/html'))
            result = await asyncio.wait_for(session.ask('Go to https://handoff.test and complete verification.'), 120)
            page = session.context.pages[-1]
            assert questions and not handoffs
            assert await page.locator('#result').inner_text() == 'Verified'
            assert await page.evaluate('window.effects') == 1
            assert '246810' not in session.store.history()[0]['inputs']
            assert '246810' not in result['answer']
            print({'passed': True, 'chat_questions': len(questions), 'browser_handoffs': len(handoffs),
                   'submissions': 1, 'answer': result['answer'], 'metrics': result['metrics']})


if __name__ == '__main__':
    asyncio.run(main())
