# Copyright 2025 IBM Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from collections.abc import Sequence

from beeai_framework.agents import AgentError, AgentExecutionConfig
from beeai_framework.agents.base import BaseAgent
from beeai_framework.agents.tool_calling.events import tool_calling_agent_event_types
from beeai_framework.agents.tool_calling.types import (
    ToolCallingAgentRunOutput,
    ToolCallingAgentRunState,
    ToolCallingAgentTemplates,
)
from beeai_framework.backend.chat import ChatModel
from beeai_framework.backend.message import (
    AssistantMessage,
    MessageToolResultContent,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from beeai_framework.context import Run, RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.memory.base_memory import BaseMemory
from beeai_framework.memory.unconstrained_memory import UnconstrainedMemory
from beeai_framework.tools.tool import AnyTool
from beeai_framework.utils.models import ModelLike, to_model

__all__ = ["ToolCallingAgent"]


class ToolCallingAgent(BaseAgent[ToolCallingAgentRunOutput]):
    def __init__(
        self,
        *,
        llm: ChatModel,
        memory: BaseMemory | None = None,
        tools: Sequence[AnyTool] | None = None,
        templates: ModelLike[ToolCallingAgentTemplates] | None = None,
    ) -> None:
        super().__init__()
        self._llm = llm
        self._memory = memory or UnconstrainedMemory()
        self._tools = tools or []
        self._templates = to_model(ToolCallingAgentTemplates, templates or {})

    def run(
        self, prompt: str | None = None, *, execution: AgentExecutionConfig | None = None
    ) -> Run[ToolCallingAgentRunOutput]:
        execution_config = execution or AgentExecutionConfig()

        async def handler(context: RunContext) -> ToolCallingAgentRunOutput:
            state = ToolCallingAgentRunState(memory=UnconstrainedMemory(), result=None, iteration=0)
            await state.memory.add(SystemMessage(self._templates.system.render()))
            await state.memory.add_many(self.memory.messages)
            if prompt is not None:
                await state.memory.add(UserMessage(prompt))

            while state.result is None:
                state.iteration += 1
                if execution_config.max_iterations and state.iteration > execution_config.max_iterations:
                    raise AgentError(f"Agent was not able to resolve the task in {state.iteration} iterations.")

                await context.emitter.emit(
                    "start",
                    {"state": state.model_dump()},
                )
                response = await self._llm.create(messages=state.memory.messages, tools=list(self._tools), stream=False)
                await state.memory.add_many(response.messages)

                tool_call_messages = response.get_tool_calls()
                for tool_call in tool_call_messages:
                    tool = next((tool for tool in self._tools if tool.name == tool_call.tool_name), None)
                    if not tool:
                        raise AgentError(f"Tool {tool_call.tool_name} does not exist!")

                    tool_input = json.loads(tool_call.args)
                    print(tool_input)
                    tool_response = await tool.run(tool_input).context(
                        {"state": state.model_dump(), "tool_call_msg": tool_call}
                    )
                    print(tool_response.get_text_content())
                    await state.memory.add(
                        ToolMessage(
                            MessageToolResultContent(
                                result=tool_response.get_text_content(),
                                tool_name=tool_call.tool_name,
                                tool_call_id=tool_call.id,
                            )
                        )
                    )

                # handle empty messages for some models
                text_messages = response.get_text_messages()
                if not tool_call_messages and not text_messages:
                    await state.memory.add(AssistantMessage("\n", {"tempMessage": True}))
                else:
                    await state.memory.delete_many(
                        [msg for msg in state.memory.messages if msg.meta.get("tempMessage", False)]
                    )

                if text_messages:
                    state.result = AssistantMessage.from_chunks(text_messages)

                await context.emitter.emit(
                    "success",
                    {"state": state.model_dump()},
                )

            await self.memory.add_many(state.memory.messages[1:])
            return ToolCallingAgentRunOutput(result=state.result, memory=state.memory)

        return self._to_run(handler, signal=None, run_params={"prompt": prompt, "execution": execution})

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["agent", "tool_calling"], creator=self, events=tool_calling_agent_event_types
        )

    @property
    def memory(self) -> BaseMemory:
        return self._memory

    @memory.setter
    def memory(self, memory: BaseMemory) -> None:
        self._memory = memory
