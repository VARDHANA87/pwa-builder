import frappe
import json
import requests
import os
from urllib.parse import urlparse
from frappe import ValidationError, _, qb, scrub, throw
from pwa_builder.rename_template_app import rename_template_app
from frappe.model.meta import Meta

@frappe.whitelist(allow_guest=True)
def add_site(data, update=False):
	if isinstance(data, str):
		data = json.loads(data)
	url = urlparse(data.get("site_url"))
	login_url = url.scheme + "://" + url.netloc + "/api/method/login"

	response = requests.post(login_url, data={"usr": data.get("user_id"), "pwd": data.get("password")})

	if response.status_code == 200:
		frappe.cache().hset(data.get("user_id"), data.get("project_title"), response.cookies)
		if not update:
			frappe.get_doc({
				"doctype": "PWA-Project",
				"project_title": data.get("project_title"),
				"sub_title": data.get("sub_title"),
				"site_url": data.get("site_url"),
				"user_id": data.get("user_id"),
				"password": data.get("password"),
				"description": data.get("description"),
			}).insert(ignore_permissions=True)

			return "Created"
		else:
			doc = frappe.get_doc("PW-Project", url.scheme + "://" + url.netloc)

			doc.update({
				"doctype": "PWA-Project",
				"project_title": data.get("project_title"),
				"sub_title": data.get("sub_title"),
				"site_url": data.get("site_url"),
				"user_id": data.get("user_id"),
				"password": data.get("password"),
				"description": data.get("description"),
				})
			data.save()
			return "Updated"
	else:
		return "Invalid credentials"

@frappe.whitelist(allow_guest=True)
def get_meta(doctype, project, cached=True) -> "Meta":
	doc = frappe.get_doc("PWA-Project", project)
	url = urlparse(doc.site_url)
	site_url = url.scheme + "://" + url.netloc
	end_point = "/api/method/frappe.desk.form.load.getdoctype?doctype={0}&with_parent=1".format(doctype)

	response = call(site_url, end_point, doc.user_id, doc.get_password("password"), doc.project_title)
	if response.ok:
		meta = response.json()
		for doc in meta["docs"]:
			if doc["name"] == doctype:
				return doc
			else:
				pass
	else:
		return "Validation Error"

def call(url, end_point, username, password, project, force=False, count=1):
	cookies = get_cookies(url, username, password, project, force=force)
	response = requests.get(url+end_point, cookies=cookies)
	if response.status_code == 403 and count <= 3:
		response = call(url, end_point, username, password, project, force=True, count=count+1)

	return response


def get_cookies(url, username, password, project,  force=False):
	cookies = frappe.cache().hget(url, project)
	if not cookies or force:
		login_url = url + "/api/method/login"
		response = requests.post(
			login_url, data={"usr": username, "pwd": password}
		)
		if response.status_code == 200:
			cookies = response.cookies
			frappe.cache().hset(url, project, cookies)
	return cookies

@frappe.whitelist(allow_guest=True)
def set_value(doctype, docname, fieldname, value):

	 frappe.set_value(doctype, docname, fieldname, json.dumps(value, indent=4))

@frappe.whitelist(allow_guest=True)
def get_doc(doctype, docname):
	 return frappe.get_doc(doctype, docname)

@frappe.whitelist(allow_guest=True)
def export_project(project_name):
	frappe.enqueue(
		method="pwa_builder.api.schedule_export_project",
		project_name=project_name,
		queue="short",
		job_name=frappe.utils.get_job_name("export_app_for", "PWA-Project", project_name)
	)

def schedule_export_project(project_name):
	from pwa_builder.pwa_builder.doctype.pwa_github_integration import pwa_github_integration
	
	#project doc
	project_doc = frappe.get_doc("PWA-Project",project_name)
	
	git_clone_response=pwa_github_integration.clone_pwa_template(project_name)
	if git_clone_response.get('success') and git_clone_response.get('public_folder_path'):
		file_path = git_clone_response.get('public_folder_path')
		if pwa_doctype := frappe.get_list("PWA DocType", {"project_name": project_doc.name}):
			for doctype in pwa_doctype:
				doc = frappe.get_doc("PWA DocType", doctype.name)
				json_data = doc.field_list
				file_name = doc.title + ".json"
				path = file_path+"/pwa_build/pwa_build/pwa_form/"+file_name.lower()
				os.makedirs(os.path.dirname(path), exist_ok=True)
				with open(path, 'w') as json_file:
					json_file.write(json_data)
			# rename the app
			if renaming_result := rename_template_app(
				app_path=file_path,
				new_app_name=project_doc.project_title,
				new_url="frontend"
			):
				if renaming_result.get("success"):
					if push_repo_result := pwa_github_integration.push_to_github(
						path=git_clone_response.get('project_folder_path')+"/"+scrub(project_doc.project_title),
						repo_name=project_doc.project_title,
						current_default_branch=project_doc.github_default_branch,
						last_push_commit=project_doc.last_push_commit
					):
						if push_repo_result.get('success'):
							project_doc.github_repository_url = push_repo_result.get("message",{}).get('clone_url',None)
							project_doc.github_default_branch = push_repo_result.get("message",{}).get('default_branch',None)
							project_doc.last_push_commit = push_repo_result.get('commit_msg')
							project_doc.save(ignore_permissions=True)
							return {"success":True, "message":"Project exported successfully"}
						else:
							return {"success" : False, "error" : push_repo_result.get('error')}
					else:
						return {"success":False, "error":"Error while pushing to github"}
				else:
					return {"success" : False, "error" : renaming_result.get('error')}
			else:
				return {"success":False, "error": "Error while renaming app"}
		else:
			return {"success" : False, "error" : "No PWA DocType found for this project"}
	else:
		return {"success":False, "error": git_clone_response.get('error')}
