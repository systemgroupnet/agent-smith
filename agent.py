from pydantic import BaseModel
import os
from dotenv import load_dotenv

# Load environment variables and setup proxy
load_dotenv()
from core.config import setup_proxy
setup_proxy()

from core.start.starter_agent import starter_agent
from core.backend.pbook_reviewer.pbook_reviewer import pbook_reviewer, PbookReviewerContext
from core.backend.sql_reviewer.sql_reviewer import sql_reviewer, SqlReviewerContext
from core.backend.service_reviewer.service_reviewer import run_combined_review, ServiceReviewerContext
from core.backend.test_reviewer.test_reviewer import test_reviewer, TestReviewerContext
from core.backend.dataview_reviewer.dataview_reviewer import dataview_reviewer, DataviewReviewerContext

class WorkflowInput(BaseModel):
  input_as_text: str


async def run_workflow(workflow_input: WorkflowInput):
    workflow = workflow_input.model_dump()
    
    # Run starter agent to classify file
    agent_result_temp = await starter_agent.run(workflow["input_as_text"])
    
    agent_result = {
        "output_text": agent_result_temp.data.model_dump_json(),
        "output_parsed": agent_result_temp.data.model_dump()
    }
    
    if agent_result["output_parsed"]["file_type"] == "pbook":
        pbook_reviewer_context = PbookReviewerContext(
            workflow_input_as_text=workflow["input_as_text"],
            content=agent_result["output_parsed"]["content"],
            diff=agent_result["output_parsed"]["diff"]
        )
        pbook_reviewer_result_temp = await pbook_reviewer.run(
            "Review this pbook file",
            deps=pbook_reviewer_context
        )
        
        pbook_reviewer_result = {
            "output_text": pbook_reviewer_result_temp.data.model_dump_json()
        }
        return pbook_reviewer_result
        
    elif agent_result["output_parsed"]["file_type"] in ["service", "validation"]:
        # Call run_combined_review to orchestrate both semantic and business logic reviewers
        combined_review_result = await run_combined_review(
            ServiceReviewerContext(
                workflow_input_as_text=workflow["input_as_text"],
                content=agent_result["output_parsed"]["content"],
                diff=agent_result["output_parsed"]["diff"],
                jira_task_number=agent_result["output_parsed"].get("jira_task_number"),
                package_name=agent_result["output_parsed"].get("package_name"),
                module_name=agent_result["output_parsed"].get("module_name"),
                file_name=agent_result["output_parsed"].get("file_name"),
                gitlab_token=os.getenv("GITLAB_TOKEN"),
                gitlab_domain=os.getenv("GITLAB_DOMAIN", "gitlab.example.com"),
                source_branch=agent_result["output_parsed"].get("source_branch"),
                project_path=os.getenv("GITLAB_PROJECT_PATH")
            )
        )
        
        service_reviewer_result = {
            "output_text": combined_review_result.model_dump_json()
        }
        return service_reviewer_result
        
    elif agent_result["output_parsed"]["file_type"] == "test":
        test_reviewer_context = TestReviewerContext(
            workflow_input_as_text=workflow["input_as_text"],
            content=agent_result["output_parsed"]["content"],
            diff=agent_result["output_parsed"]["diff"]
        )
        test_reviewer_result_temp = await test_reviewer.run(
            "Review this test file",
            deps=test_reviewer_context
        )
        
        test_reviewer_result = {
            "output_text": test_reviewer_result_temp.data.model_dump_json()
        }
        return test_reviewer_result
        
    elif agent_result["output_parsed"]["file_type"] == "sql":
        sql_reviewer_context = SqlReviewerContext(
            workflow_input_as_text=workflow["input_as_text"],
            content=agent_result["output_parsed"]["content"],
            diff=agent_result["output_parsed"]["diff"]
        )
        sql_reviewer_result_temp = await sql_reviewer.run(
            "Review this SQL file",
            deps=sql_reviewer_context
        )
        
        sql_reviewer_result = {
            "output_text": sql_reviewer_result_temp.data.model_dump_json()
        }
        return sql_reviewer_result
        
    elif agent_result["output_parsed"]["file_type"] == "dataview":
        dataview_reviewer_context = DataviewReviewerContext(
            workflow_input_as_text=workflow["input_as_text"],
            content=agent_result["output_parsed"]["content"],
            diff=agent_result["output_parsed"]["diff"],
            jira_task_number=agent_result["output_parsed"].get("jira_task_number"),
            package_name=agent_result["output_parsed"].get("package_name"),
            module_name=agent_result["output_parsed"].get("module_name"),
            file_name=agent_result["output_parsed"].get("file_name"),
            gitlab_token=os.getenv("GITLAB_TOKEN"),
            gitlab_domain=os.getenv("GITLAB_DOMAIN", "gitlab.example.com"),
            source_branch=agent_result["output_parsed"].get("source_branch"),
            project_path=os.getenv("GITLAB_PROJECT_PATH")
        )
        dataview_reviewer_result_temp = await dataview_reviewer.run(
            "Review this dataview file",
            deps=dataview_reviewer_context
        )
        
        dataview_reviewer_result = {
            "output_text": dataview_reviewer_result_temp.data.model_dump_json()
        }
        return dataview_reviewer_result
        
    else:
        return agent_result
