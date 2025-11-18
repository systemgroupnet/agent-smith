from agents import TResponseInputItem, Runner, RunConfig, trace
from pydantic import BaseModel
import os
from dotenv import load_dotenv

# Load environment variables and setup proxy
load_dotenv()
from core.config import setup_proxy
setup_proxy()

from core.start import starter_agent
from core.backend.pbook_reviewer import pbook_reviewer, PbookReviewerContext
from core.backend.sql_reviewer import sql_reviewer, SqlReviewerContext
from core.backend.service_reviewer import run_combined_review, ServiceReviewerContext
from core.backend.test_reviewer import test_reviewer, TestReviewerContext
from core.backend.dataview_reviewer import dataview_reviewer, DataviewReviewerContext

class WorkflowInput(BaseModel):
  input_as_text: str


async def run_workflow(workflow_input: WorkflowInput):
  with trace("CRASG"):
    state = {

    }
    workflow = workflow_input.model_dump()
    conversation_history: list[TResponseInputItem] = [
      {
        "role": "user",
        "content": [
          {
            "type": "input_text",
            "text": workflow["input_as_text"]
          }
        ]
      }
    ]
    agent_result_temp = await Runner.run(
      starter_agent,
      input=[
        *conversation_history
      ],
      run_config=RunConfig(trace_metadata={
        "__trace_source__": "agent-builder",
        "workflow_id": "wf_68e5e4144d2881909eeb7d90b7b8d77408a3a1d85b9ede4c"
      })
    )

    conversation_history.extend([item.to_input_item() for item in agent_result_temp.new_items])

    agent_result = {
      "output_text": agent_result_temp.final_output.json(),
      "output_parsed": agent_result_temp.final_output.model_dump()
    }
    if agent_result["output_parsed"]["file_type"] == "pbook":
      pbook_reviewer_result_temp = await Runner.run(
        pbook_reviewer,
        input=[
          *conversation_history
        ],
        run_config=RunConfig(trace_metadata={
          "__trace_source__": "agent-builder",
          "workflow_id": "wf_68e5e4144d2881909eeb7d90b7b8d77408a3a1d85b9ede4c"
        }),
        context=PbookReviewerContext(
          workflow_input_as_text=workflow["input_as_text"],
          content=agent_result["output_parsed"]["content"],
          diff=agent_result["output_parsed"]["diff"]
        )
      )

      conversation_history.extend([item.to_input_item() for item in pbook_reviewer_result_temp.new_items])

      pbook_reviewer_result = {
        "output_text": pbook_reviewer_result_temp.final_output_as(str)
      }
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
    elif agent_result["output_parsed"]["file_type"] == "test":
      test_reviewer_result_temp = await Runner.run(
        test_reviewer,
        input=[
          *conversation_history
        ],
        run_config=RunConfig(trace_metadata={
          "__trace_source__": "agent-builder",
          "workflow_id": "wf_68e5e4144d2881909eeb7d90b7b8d77408a3a1d85b9ede4c"
        }),
        context=TestReviewerContext(
          workflow_input_as_text=workflow["input_as_text"],
          content=agent_result["output_parsed"]["content"],
          diff=agent_result["output_parsed"]["diff"]
        )
      )

      conversation_history.extend([item.to_input_item() for item in test_reviewer_result_temp.new_items])

      test_reviewer_result = {
        "output_text": test_reviewer_result_temp.final_output_as(str)
      }
    elif agent_result["output_parsed"]["file_type"] == "sql":
      sql_reviewer_result_temp = await Runner.run(
        sql_reviewer,
        input=[
          *conversation_history
        ],
        run_config=RunConfig(trace_metadata={
          "__trace_source__": "agent-builder",
          "workflow_id": "wf_68e5e4144d2881909eeb7d90b7b8d77408a3a1d85b9ede4c"
        }),
        context=SqlReviewerContext(
          workflow_input_as_text=workflow["input_as_text"],
          content=agent_result["output_parsed"]["content"],
          diff=agent_result["output_parsed"]["diff"]
        )
      )

      conversation_history.extend([item.to_input_item() for item in sql_reviewer_result_temp.new_items])

      sql_reviewer_result = {
        "output_text": sql_reviewer_result_temp.final_output_as(str)
      }
    elif agent_result["output_parsed"]["file_type"] == "dataview":
      dataview_reviewer_result_temp = await Runner.run(
        dataview_reviewer,
        input=[
          *conversation_history
        ],
        run_config=RunConfig(trace_metadata={
          "__trace_source__": "agent-builder",
          "workflow_id": "wf_68e5e4144d2881909eeb7d90b7b8d77408a3a1d85b9ede4c"
        }),
        context=DataviewReviewerContext(
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

      conversation_history.extend([item.to_input_item() for item in dataview_reviewer_result_temp.new_items])

      dataview_reviewer_result = {
        "output_text": dataview_reviewer_result_temp.final_output.json()
      }
    else:
      return agent_result
