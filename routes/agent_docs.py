"""Public, inline documentation at a fixed allowlist of paths."""
from pathlib import Path

from flask import Blueprint, Response, current_app, redirect, render_template


agent_docs_bp = Blueprint('agent_docs', __name__)

# Route defaults are the only selectors; no client-controlled filesystem paths.
DOCUMENT_PATHS = {
    'skill': ('skill.md',),
    'api': ('docs', 'agent-api.md'),
    'prompt': ('docs', 'agent-prompt.md'),
}


@agent_docs_bp.route('/ai-assistant')
def agent_guide():
    # Reuse the maintained brief so the human guide and agent instructions agree.
    # This public page never creates an identity, permission grant or routine.
    try:
        document = Path(current_app.root_path, *DOCUMENT_PATHS['prompt']).read_text(encoding='utf-8')
        introduction, separator, brief = document.partition('\n---\n')
        prompt = (brief if separator else introduction).strip() or None
    except OSError:
        prompt = None
    return render_template('agent_guide.html', agent_prompt=prompt)


@agent_docs_bp.route('/skill.md', defaults={'document': 'skill'}, endpoint='skill_md')
@agent_docs_bp.route('/SKILL.md', defaults={'document': 'skill'}, endpoint='skill_md_upper')
@agent_docs_bp.route('/skill_api.md', defaults={'document': 'api'}, endpoint='skill_api')
@agent_docs_bp.route('/api/docs.md', defaults={'document': 'api'}, endpoint='skill_api_alias')
@agent_docs_bp.route('/skill_prompt.md', defaults={'document': 'prompt'}, endpoint='skill_prompt')
def markdown_document(document):
    relative_path = DOCUMENT_PATHS[document]
    try:
        content = Path(current_app.root_path, *relative_path).read_text(encoding='utf-8')
        status = 200
    except OSError:
        content = 'Agent documentation is unavailable. Please try again later.\n'
        status = 404
    response = Response(content, status=status, mimetype='text/plain')
    response.headers['Content-Disposition'] = f'inline; filename="{relative_path[-1]}"'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@agent_docs_bp.route('/api/docs')
def api_docs():
    response = redirect('/skill_api.md', code=302)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
