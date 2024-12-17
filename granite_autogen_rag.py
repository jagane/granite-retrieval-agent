"""
requirements: autogen
"""
from datetime import date, datetime
from autogen import coding, ConversableAgent
from typing import Annotated, Any, Optional, Callable, Awaitable
from open_webui.apps.retrieval import main
from open_webui.apps.webui.models.knowledge import KnowledgeTable
from pydantic import BaseModel, Field
import json
import logging
from langchain_community.utilities import SearxSearchWrapper

####################
# Assistant prompts
####################
PLANNER_MESSAGE = (
    """You are a task planner. You will be given some information your job is to think step by step and enumerate the steps to complete a performance assessment of a given user, using the provided context to guide you.
    You will not execute the steps yourself, but provide the steps to a helper who will execute them. Make sure each step consists of a single operation, not a series of operations. The helper has the following capabilities:
    1. Search through a collection of documents provided by the user. These are the user's own documents and will likely not have latest news or other information you can find on the internet.
    2. Synthesize, summarize and classify the information received.
    3. Search the internet
    Please output the step using a properly formatted python dictionary and list. Respond only with the plan json as described below and no additional text. Here are a few examples:
    Example 1: 
    User query: Write a performance self-assessment for Joe, consisting of a high-level overview of achievements for the year, a listing of the business impacts for each of these achievements, a list of skills developed and ways he's collaborated with the team.
    Your response:
    ```{"plan": ["Query documents for all contributions involving Joe this year", "Quantify the business impact for Joe's contributions", "Enumerate the skills Joe has developed this year", "List several examples of how Joe's work has been accomplished via team collaboration", "Formulate the performance review based on collected information"]}```

    Example 2:
    User query: Find the latest news about the technologies I'm working on.
    Your response:
    ```{"plan": ["Query documents for technologies used", "Search the internet for the latest news about each technology"]}```
    """
)

ASSISTANT_PROMPT = (
    """You are an AI assistant.
    When you receive a message, figure out a solution and provide a final answer. The message will be accompanied with contextual information. Use the contextual information to help you provide a solution.
    Make sure to provide a thorough answer that directly addresses the message you received.
    The context may contain extraneous information that does not apply to your instruction. If so, just extract whatever is useful or relevant and use it to complete your instruction.
    When the context does not include enough information to complete the task, use your available tools to retrieve the specific information you need.
    When you are using knowledge and web search tools to complete the instruction, answer the instruction only using the results from the search; do no supplement with your own knowledge.
    Be persistent in finding the information you need before giving up.
    If the task is able to be accomplished without using tools, then do not make any tool calls.
    When you have accomplished the instruction posed to you, you will reply with the text: ##SUMMARY## - followed with an answer.
    Important: If you are unable to accomplish the task, whether it's because you could not retrieve sufficient data, or any other reason, reply only with ##TERMINATE##.

    # Tool Use
    You have access to the following tools. Only use these available tools and do not attempt to use anything not listed - this will cause an error.
    Respond in the format: <function_call> {"name": function name, "arguments": dictionary of argument name and its value}. Do not use variables.
    Only call one tool at a time.
    When suggesting tool calls, please respond with a JSON for a function call with its proper arguments that best answers the given prompt.
    """
)

REFLECTION_ASSISTANT_PROMPT = (
    """You are an assistant. Please tell me what is the next step that needs to be taken in a plan in order to accomplish a given task.
    You will receive json in the following format, and will respond with a single line of instruction.

    {
        "Goal": The original query from the user. Every time you create a reply, it must be guided by the task of fulfilling this goal. Do not veer off course.,
        "Plan": An array that enumerates every step of the plan,
        "Previous Step": The step taken immediately prior to this message.
        "Previous Output": The output generated by the last step taken.
        "Steps Taken": A sequential array of steps that have already been executed prior to the last step,

    }

    Instructions:
        1. If the very last step of the plan has already been executed, or the goal has already been achieved regardless of what step is next, then reply with the exact text: ##TERMINATE##
        2. Look at the "Previous Step". If the last step was not successful and it is integral to solving the next step of the plan, do not move onto the next step. Inspect why the previous step was not successful, and modify the instruction to find another way to achieve the step's objective in a way that won't repeat the same error.
        3. If the last previous was successful, determine what the next step will be. Always prefer to execute the next sequential step in the plan unless the previous step was unsuccessful and you need to re-run the previous step using a modified instruction.
        4. When determining the next step, you may use the "Previous Step", "Previous Output", and "Steps Taken" to give you contextual information to decide what next step to take.

    Be persistent and resourceful to make sure you reach the goal.
    """
)

CRITIC_PROMPT = (
    """The previous instruction was {last_step} \nThe following is the output of that instruction.
    if the output of the instruction completely satisfies the instruction, then reply with ##YES##.
    For example, if the instruction is to list companies that use AI, then the output contains a list of companies that use AI.
    If the output contains the phrase 'I'm sorry but...' then it is likely not fulfilling the instruction. \n
    If the output of the instruction does not properly satisfy the instruction, then reply with ##NO## and the reason why.
    For example, if the instruction was to list companies that use AI but the output does not contain a list of companies, or states that a list of companies is not available, then the output did not properly satisfy the instruction.
    If it does not satisfy the instruction, please think about what went wrong with the previous instruction and give me an explanation along with the text ##NO##. \n
    Previous step output: \n {last_output}"""
)

class Pipe:
    class Valves(BaseModel):
        SEARX_HOST: str = Field(default="http://127.0.0.1:8888")
        TASK_MODEL_ID: str = Field(default="granite3.1-instruct_4k:8b")
        OPENAI_API_URL: str = Field(default="http://localhost:11434/v1")
        OPENAI_API_KEY: str = Field(default="ollama")
        MODEL_TEMPERATURE: float = Field(default=0)
        MAX_PLAN_STEPS: int = Field(default=6)

    def __init__(self):
        self.type = "pipe"
        self.id = "granite_retrieval_agent"
        self.name = "Granite Retrieval Agent"
        self.valves = self.Valves()

    def get_provider_models(self):
        return [
            {"id": self.valves.TASK_MODEL_ID, "name": self.valves.TASK_MODEL_ID},
        ]

    def is_open_webui_request(self, body):
        # only look at last message
        message = str(body[-1])
        if "Create a concise, 3-5 word title with an emoji as a title for the chat history" in message or \
                "Generate 1-3 broad tags categorizing the main themes of the chat history, along with 1-3 more specific subtopic tags." in message or \
                    "You are an autocompletion system." in message:
            return True
        return False

    async def emit_event_safe(self, message):
        event_data = {
                        "type": "message",
                        "data": {"content": message + "\n"},
                    }
        try:
            start_time = datetime.now()
            await self.event_emitter(event_data)
        except Exception as e:
            logging.error(f"Error emitting event: {e}")

    def parse_response(self, message: str) -> dict[str, Any]:
        """
        Parse the response from the planner and return the response as a dictionary.
        """
        # Parse the response content
        json_response = {}
        # if message starts with ``` and ends with ``` then remove them
        if message.startswith("```"):
            message = message[3:]
        if message.endswith("```"):
            message = message[:-3]
        if message.startswith("json"):
            message = message[4:]
        if message.startswith("python"):
            message = message[6:]
        message = message.strip()
        try:
            json_response: dict[str, Any] = json.loads(message)
        except Exception as e:
            # If the response is not a valid JSON, try pass it using string matching.
            # This should seldom be triggered
            print(f"LLM response was not properly formed JSON. Will try to use it as is. LLM response: \"{message}\". Error: {e}")
            message = message.replace("\\n", "\n")
            message = message.replace("\n", " ")  # type: ignore
            if ("plan" in message and "next_step" in message):
                start = message.index("plan") + len("plan")
                end = message.index("next_step")
                json_response["plan"] = message[start:end].replace('"', '').strip()

        return json_response

    async def pipe(
        self,
        body,
        __user__: Optional[dict] = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
    ) -> str:

        # Grab env variables
        searx_host = self.valves.SEARX_HOST
        default_model = self.valves.TASK_MODEL_ID
        base_url = self.valves.OPENAI_API_URL
        api_key = self.valves.OPENAI_API_KEY
        model_temp = self.valves.MODEL_TEMPERATURE
        max_plan_steps = self.valves.MAX_PLAN_STEPS
        self.event_emitter = __event_emitter__

        ##################
        # AutoGen Config
        ##################
        # LLM Config
        llm_config = {
            "config_list": [{
                "model": default_model,
                "base_url": base_url,
                "api_key": api_key,
                "cache_seed": None,
                "price": [0.0, 0.0],
            }],
            "temperature": model_temp,
        }

        # Generic Assistant - Used for general inquiry. Does not call tools.
        generic_assistant = ConversableAgent(
            name="Generic_Assistant",
            llm_config=llm_config,
            human_input_mode="NEVER"
        )

        # Provides the initial high level plan
        planner = ConversableAgent(
            name="Planner",
            system_message=PLANNER_MESSAGE,
            llm_config=llm_config,
            human_input_mode="NEVER"
        )

        # The assistant agent is responsible for executing each step of the plan, including calling tools
        assistant = ConversableAgent(
            name="Research_Assistant",
            system_message=ASSISTANT_PROMPT,
            llm_config=llm_config,
            human_input_mode="NEVER",
            is_termination_msg=lambda msg: "tool_response" not in msg and msg["content"] == ""
        )

        # Reflection Assistant: Reflect on plan progress and give the next step
        reflection_assistant = ConversableAgent(
            name="ReflectionAssistant",
            system_message=REFLECTION_ASSISTANT_PROMPT,
            llm_config=llm_config,
            human_input_mode="NEVER"
        )

        # User Proxy chats with assistant on behalf of user and executes tools
        code_exec = coding.LocalCommandLineCodeExecutor(
            timeout=10,
            work_dir="code_exec",
        )
        user_proxy = ConversableAgent(
            name="User",
            human_input_mode="NEVER",
            code_execution_config={"executor": code_exec},
            is_termination_msg=lambda msg: "##SUMMARY##" in msg["content"] or "## Summary" in msg["content"] or "##TERMINATE##" in msg["content"] or ("tool_calls" not in msg and msg["content"] == "")
        )

        ##################
        # Check if this request is utility call from OpenWebUI
        ##################
        if self.is_open_webui_request(body["messages"]):
            print("Is open webui request")
            reply = generic_assistant.generate_reply(messages=[body['messages'][-1]])
            return reply

        ##################
        # Tool Definitions
        ##################
        @assistant.register_for_llm(name="web_search", description="Searches the web according to a given query")
        @user_proxy.register_for_execution(name="web_search")
        def do_web_search(search_instruction: Annotated[str, "search instruction"]) -> str:
            """This function is used for searching the web for information that can only be found on the internet, not in the users personal notes.
            """
            if not search_instruction:
                return "Please provide a search query."

            # First, we convert the incoming query into a search term.
            today = date.today().strftime("%Y-%m-%d")

            chat_result = user_proxy.initiate_chat(
                recipient=generic_assistant,
                message="Given the user's message, suggest a search term to best fulfill their query. Make sure you are understanding the intent of their question. Today's date is " + today + ". " + search_instruction,
                max_turns=1,
            )
            summary = chat_result.chat_history[-1]['content']

            search = SearxSearchWrapper(searx_host=searx_host)

            response = search.run(query=summary)
            return response

        @assistant.register_for_llm(name="personal_knowledge_search", description="Searches personal documents according to a given query")
        @user_proxy.register_for_execution(name="personal_knowledge_search")
        def do_knowledge_search(search_instruction: Annotated[str, "search instruction"]) -> str:
            """Given an instruction on what knowledge you need to find, search the user's documents for information particular to them, their projects, and their domain.
            This is simple document search, it cannot perform any other complex tasks.
            This will not give you any results from the internet. Do not assume it can retrieve the latest news pertaining to any subject."""
            if not search_instruction:
                return "Please provide a search query."

            # First get all the user's knowledge bases associated with the model
            knowledge_item_list = KnowledgeTable().get_knowledge_bases()
            if len(knowledge_item_list) == 0:
                return "You don't have any knowledge bases."
            collection_list = []
            for item in knowledge_item_list:
                collection_list.append(item.id)

            collection_form = main.QueryCollectionsForm(
                collection_names=collection_list,
                query=search_instruction
            )

            response = main.query_collection_handler(collection_form)
            messages = ""
            for entries in response['documents']:
                for line in entries:
                    messages += line

            return messages

        #########################
        # Begin Agentic Workflow
        #########################

        # Make a plan
        await self.emit_event_safe(message="Creating a plan...")
        raw_plan = user_proxy.initiate_chat(message=body['messages'][-1], max_turns=1, recipient=planner).chat_history[-1]["content"]
        plan_dict = self.parse_response(raw_plan)

        # Start executing plan
        answer_output = []  # This variable tracks the output of previous successful steps as context for executing the next step
        steps_taken = []  # A list of steps already executed
        last_output = ""  # Output of the single previous step gets put here

        for _ in range(max_plan_steps):
            if last_output == "":
                # This is the first step of the plan since there's no previous output
                instruction = plan_dict['plan'][0]
            else:
                # Previous steps in the plan have already been executed.
                await self.emit_event_safe(message="Planning the next step...")
                reflection_message = last_step
                # Ask the critic if the previous step was properly accomplished
                was_job_accomplished = user_proxy.initiate_chat(recipient=generic_assistant, max_turns=1,
                                                                message=CRITIC_PROMPT.format(last_step=last_step, last_output=last_output)).chat_history[-1]["content"]
                # If it was not accomplished, make sure an explanation is provided for the reflection assistant
                if "##NO##" in was_job_accomplished:
                    reflection_message = f"The previous step was {last_step} but it was not accomplished satisfactorily due to the following reason: \n {was_job_accomplished}."

                # Then, ask the reflection agent for the next step
                message = {
                    "Goal": body['messages'][-1],
                    "Plan": str(plan_dict),
                    "Last Step": reflection_message,
                    "Last Step Output": str(last_output),
                    "Steps Taken": str(steps_taken),
                }
                instruction = user_proxy.initiate_chat(recipient=reflection_assistant, max_turns=1, message=str(message)).chat_history[-1]["content"]

                # Only append the previous step and its output to the record if it accomplished its task successfully.
                # It was found that storing information about unsuccesful steps causes more confusion than help to the agents
                if not "##NO##" in was_job_accomplished:
                    answer_output.append(last_output)
                    steps_taken.append(last_step)

                if "##TERMINATE##" in instruction:
                    # A termination message means there are no more steps to take. Exit the loop.
                    break

            # Now that we have determined the next step to take, execute it
            await self.emit_event_safe(message="Executing step: " + instruction)
            prompt = instruction
            if answer_output:
                prompt += f"\n Contextual Information: \n{answer_output}"
            output = user_proxy.initiate_chat(recipient=assistant, max_turns=3, message=prompt)

            # Sort through the chat history and extract out replies from the assistant (We don't need the full results of the tool calls, just the assistant's summary)
            previous_output = []
            for chat_item in output.chat_history:
                if chat_item["content"] and chat_item["name"] == "Research_Assistant":
                    previous_output.append(chat_item["content"])
            
            # It was found in testing that the output of the assistant will often contain the right information, but it will not be formatted in a manner that directly answers the instruction
            # Therefore, the critic will take the assistant's output and reformat it to more directly answer the instruction that was given to the assistant
            critic_output = user_proxy.initiate_chat(recipient=generic_assistant, max_turns=1, message=f"The instruction is: {instruction} Please directly answer the instruction given the following data: {previous_output}")

            # The previous instruction and its output will be recorded for the next iteration to inspect before determining the next step of the plan
            last_output = critic_output.chat_history[-1]["content"]
            last_step = instruction
        
        await self.emit_event_safe(message="Summing up findings...")
        # Now that we've gathered all the information we need, we will summarize it to directly answer the original prompt
        final_prompt = f"Answer the user's query: {body['messages'][-1]}. Using the following contextual informaiton only: {answer_output}"
        final_output = user_proxy.initiate_chat(message=final_prompt, max_turns=1, recipient=generic_assistant).chat_history[-1]["content"]

        return(final_output)