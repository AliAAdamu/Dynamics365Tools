"""All Flask route handlers — flat module, no sub-package."""
import os

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from auth import clear_token_cache, get_access_token
from config_manager import (
    UPLOADS_DIR,
    delete_environment,
    delete_plan,
    get_client_secret,
    get_environment,
    get_plan,
    get_result,
    list_environments,
    list_plans,
    list_results,
    save_environment,
    save_plan,
)
from dmf_client import DMFClient
from runner import get_run_status, start_run

bp = Blueprint("main", __name__)

_ALLOWED_EXTENSIONS = {".zip", ".xml", ".csv", ".txt"}


# ─── Dashboard ───────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    return render_template(
        "index.html",
        env_count=len(list_environments()),
        plan_count=len(list_plans()),
        recent_results=list_results(limit=5),
    )


# ─── Environments ─────────────────────────────────────────────────────────────

@bp.route("/environments")
def environments():
    return render_template("environments.html", environments=list_environments())


@bp.route("/environments/new")
def environment_new():
    return render_template("environment_form.html", env=None)


@bp.route("/environments/<env_id>/edit")
def environment_edit(env_id):
    env = get_environment(env_id)
    if not env:
        flash("Environment not found.", "danger")
        return redirect(url_for("main.environments"))
    return render_template("environment_form.html", env=env)


@bp.route("/environments/save", methods=["POST"])
def environment_save():
    data = {
        "id": request.form.get("id", "").strip() or None,
        "name": request.form["name"].strip(),
        "base_url": request.form["base_url"].strip().rstrip("/"),
        "tenant_id": request.form["tenant_id"].strip(),
        "client_id": request.form["client_id"].strip(),
    }
    secret = request.form.get("client_secret", "").strip()
    if secret:
        data["client_secret"] = secret

    if not all([data["name"], data["base_url"], data["tenant_id"], data["client_id"]]):
        flash("All fields except client secret are required.", "danger")
        return render_template("environment_form.html", env=data)

    save_environment(data)
    clear_token_cache(data.get("tenant_id"), data.get("client_id"))
    flash(f"Environment '{data['name']}' saved.", "success")
    return redirect(url_for("main.environments"))


@bp.route("/environments/<env_id>/delete", methods=["POST"])
def environment_delete(env_id):
    env = get_environment(env_id)
    if env:
        clear_token_cache(env.get("tenant_id"), env.get("client_id"))
        delete_environment(env_id)
        flash(f"Environment '{env['name']}' deleted.", "success")
    return redirect(url_for("main.environments"))


# ─── Test connection ──────────────────────────────────────────────────────────

@bp.route("/api/environments/<env_id>/test", methods=["POST"])
def environment_test(env_id):
    env = get_environment(env_id)
    if not env:
        return jsonify({"ok": False, "error": "Environment not found"}), 404
    try:
        secret = get_client_secret(env)
        token = get_access_token(env["tenant_id"], env["client_id"], secret, env["base_url"])
        client = DMFClient(env["base_url"], token)
        groups = client.list_definition_groups()
        return jsonify({"ok": True, "definition_groups": len(groups)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 200


@bp.route("/api/environments/<env_id>/definition-groups")
def definition_groups(env_id):
    env = get_environment(env_id)
    if not env:
        return jsonify([])
    try:
        secret = get_client_secret(env)
        token = get_access_token(env["tenant_id"], env["client_id"], secret, env["base_url"])
        client = DMFClient(env["base_url"], token)
        return jsonify(client.list_definition_groups())
    except Exception:  # noqa: BLE001
        return jsonify([])


# ─── Test Plans ───────────────────────────────────────────────────────────────

@bp.route("/plans")
def plans():
    return render_template("plans.html", plans=list_plans(), environments=list_environments())


@bp.route("/plans/new")
def plan_new():
    return render_template("plan_form.html", plan=None, environments=list_environments())


@bp.route("/plans/<plan_id>/edit")
def plan_edit(plan_id):
    plan = get_plan(plan_id)
    if not plan:
        flash("Test plan not found.", "danger")
        return redirect(url_for("main.plans"))
    return render_template("plan_form.html", plan=plan, environments=list_environments())


@bp.route("/plans/save", methods=["POST"])
def plan_save():
    operation = request.form.get("operation", "import")
    file_path = request.form.get("existing_file_path", "").strip()

    uploaded = request.files.get("import_file")
    if uploaded and uploaded.filename:
        ext = os.path.splitext(secure_filename(uploaded.filename))[1].lower()
        if ext not in _ALLOWED_EXTENSIONS:
            flash(f"File type '{ext}' not allowed. Use: {', '.join(_ALLOWED_EXTENSIONS)}", "danger")
            return redirect(request.referrer or url_for("main.plans"))
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        filename = secure_filename(uploaded.filename)
        file_path = os.path.join(UPLOADS_DIR, filename)
        uploaded.save(file_path)

    data = {
        "id": request.form.get("id", "").strip() or None,
        "name": request.form["name"].strip(),
        "description": request.form.get("description", "").strip(),
        "operation": operation,
        "definition_group_id": request.form.get("definition_group_id", "").strip(),
        "legal_entity": request.form.get("legal_entity", "").strip(),
        "poll_interval": int(request.form.get("poll_interval", 5)),
        "poll_timeout": int(request.form.get("poll_timeout", 600)),
    }

    if operation == "import":
        data["file_path"] = file_path
        data["execute_immediately"] = bool(request.form.get("execute_immediately"))
        data["overwrite"] = bool(request.form.get("overwrite"))
    else:
        data["package_name"] = request.form.get("package_name", "").strip()
        data["re_execute"] = bool(request.form.get("re_execute"))

    if not all([data["name"], data["definition_group_id"], data["legal_entity"]]):
        flash("Name, Definition Group ID, and Legal Entity are required.", "danger")
        return render_template("plan_form.html", plan=data, environments=list_environments())

    save_plan(data)
    flash(f"Test plan '{data['name']}' saved.", "success")
    return redirect(url_for("main.plans"))


@bp.route("/plans/<plan_id>/duplicate", methods=["POST"])
def plan_duplicate(plan_id):
    plan = get_plan(plan_id)
    if not plan:
        flash("Test plan not found.", "danger")
        return redirect(url_for("main.plans"))
    import copy
    new_plan = copy.deepcopy(plan)
    new_plan.pop("id", None)
    new_plan.pop("created_at", None)
    new_plan.pop("updated_at", None)
    new_plan["name"] = f"Copy of {plan['name']}"
    save_plan(new_plan)
    flash(f"Test plan duplicated as '{new_plan['name']}'.", "success")
    return redirect(url_for("main.plans"))


@bp.route("/plans/<plan_id>/delete", methods=["POST"])
def plan_delete(plan_id):
    plan = get_plan(plan_id)
    if plan:
        delete_plan(plan_id)
        flash(f"Test plan '{plan['name']}' deleted.", "success")
    return redirect(url_for("main.plans"))


# ─── Runner ───────────────────────────────────────────────────────────────────

@bp.route("/run")
def runner():
    return render_template("runner.html", environments=list_environments(), plans=list_plans())


@bp.route("/api/run", methods=["POST"])
def api_run():
    body = request.get_json(force=True) or {}
    plan_id = body.get("plan_id", "").strip()
    env_id = body.get("env_id", "").strip()
    iterations = max(1, min(int(body.get("iterations", 1)), 50))
    mode = body.get("mode", "serial")
    if mode not in ("parallel", "serial"):
        mode = "serial"

    if not plan_id or not env_id:
        return jsonify({"error": "plan_id and env_id are required"}), 400
    if not get_plan(plan_id):
        return jsonify({"error": "Plan not found"}), 404
    if not get_environment(env_id):
        return jsonify({"error": "Environment not found"}), 404

    run_id = start_run(plan_id, env_id, iterations, mode)
    return jsonify({"run_id": run_id})


@bp.route("/api/run/<run_id>")
def api_run_status(run_id):
    status = get_run_status(run_id)
    if status is None:
        result = get_result(run_id)
        if result:
            return jsonify(result)
        return jsonify({"error": "Run not found"}), 404
    return jsonify(status)


# ─── Results ──────────────────────────────────────────────────────────────────

@bp.route("/results")
def results():
    return render_template("results.html", results=list_results())


@bp.route("/results/<result_id>")
def result_detail(result_id):
    result = get_result(result_id)
    if not result:
        flash("Result not found.", "danger")
        return redirect(url_for("main.results"))
    return render_template("result_detail.html", result=result)


@bp.route("/results/<result_id>/download/<path:execution_id>")
def result_download(result_id, execution_id):
    """Fetch the SAS download URL for an exported DMF package and redirect to it."""
    result = get_result(result_id)
    if not result:
        flash("Result not found.", "danger")
        return redirect(url_for("main.results"))

    env = get_environment(result["environment_id"])
    if not env:
        flash("Environment not found.", "danger")
        return redirect(url_for("main.result_detail", result_id=result_id))

    try:
        secret = get_client_secret(env)
        token = get_access_token(env["tenant_id"], env["client_id"], secret, env["base_url"])
        client = DMFClient(env["base_url"], token)
        download_url = client.get_exported_file_url(execution_id)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not retrieve file URL: {exc}", "danger")
        return redirect(url_for("main.result_detail", result_id=result_id))

    return redirect(download_url)


@bp.route("/results/<result_id>/delete", methods=["POST"])
def result_delete(result_id):
    from config_manager import RESULTS_DIR
    path = os.path.join(RESULTS_DIR, f"{result_id}.json")
    if os.path.exists(path):
        os.remove(path)
        flash("Result deleted.", "success")
    return redirect(url_for("main.results"))
