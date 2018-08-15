from warnings import warn as _warn
from cryptography.fernet import Fernet
import cloudpickle
from collections import Counter
import xxhash
import copy
import inspect
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    AnyStr,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

import graphviz
from mypy_extensions import TypedDict

import prefect
import prefect.schedules
from prefect.core.edge import Edge
from prefect.core.task import Parameter, Task
from prefect.utilities.json import Serializable, dumps
from prefect.utilities.tasks import as_task
from prefect.environments import Environment

ParameterDetails = TypedDict("ParameterDetails", {"default": Any, "required": bool})


class Flow(Serializable):
    """
    The Flow class is used as the representation of a collection of dependent Tasks.
    Flows track Task dependencies, parameters and provide the main API for constructing and managing workflows.

    Initializing Flow example:
    ```python
    class MyTask(Task):
        def run(self):
            return "hello"

    task_1 = MyTask()
    flow = Flow(name="my_flow", tasks=[task_1])

    flow.run()
    ```

    Initializing Flow as context manager example:
    ```python
    @task
    def my_task():
        return "hello"

    with Flow("my_flow") as flow:
        task_1 = my_task()

    flow.run()
    ```

    Args:
        - name (str, optional): The name of the flow
        - version (str, optional): The flow's version
        - project (str, optional): The flow's project
        - schedule (prefect.schedules.Schedule, optional): A schedule used to
        represent when the flow should run
        - description (str, optional): Descriptive information about the flow
        - environment (prefect.environments.Environment, optional): The environment
        type that the flow should be run in
        - tasks ([Task], optional): If provided, a list of tasks that will initialize the flow
        - edges ([Edge], optional): A list of edges between tasks
        - key_tasks ([Task], optional): A list of tasks which determine the final
        state of a flow
        - register (bool, optional): Whether or not to add the flow to the registry
    """

    def __init__(
        self,
        name: str = None,
        version: str = None,
        project: str = None,
        schedule: prefect.schedules.Schedule = None,
        description: str = None,
        environment: Environment = None,
        tasks: Iterable[Task] = None,
        edges: Iterable[Edge] = None,
        key_tasks: Iterable[Task] = None,
        register: bool = False,
    ) -> None:
        self._id = str(uuid.uuid4())
        self._task_ids = dict()  # type: Dict[Task, str]

        self.name = name or type(self).__name__
        self.version = version or prefect.config.flows.default_version
        self.project = project or prefect.config.flows.default_project
        self.description = description or None
        self.schedule = schedule or prefect.schedules.NoSchedule()
        self.environment = environment

        self.tasks = set()  # type: Set[Task]
        self.edges = set()  # type: Set[Edge]

        for t in tasks or []:
            self.add_task(t)

        for e in edges or []:
            self.add_edge(
                upstream_task=e.upstream_task,
                downstream_task=e.downstream_task,
                key=e.key,
            )
        self.set_key_tasks(key_tasks or [])

        self._prefect_version = prefect.__version__

        if register:
            self.register()

        super().__init__()

    def __eq__(self, other: Any) -> bool:
        if type(self) == type(other):
            s = (
                self.project,
                self.name,
                self.version,
                self.tasks,
                self.edges,
                self.key_tasks(),
            )
            o = (
                other.project,
                other.name,
                other.version,
                other.tasks,
                other.edges,
                other.key_tasks(),
            )
            return s == o
        return False

    def __repr__(self) -> str:
        template = (
            "<{cls}: project={self.project}, name={self.name}, version={self.version}>"
        )
        return template.format(cls=type(self).__name__, self=self)

    def __iter__(self) -> Iterable[Task]:
        yield from self.sorted_tasks()

    def copy(self) -> "Flow":
        new = copy.copy(self)
        new.tasks = self.tasks.copy()
        new.edges = self.edges.copy()
        new.set_key_tasks(self._key_tasks)
        return new

    # Identification -----------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    def key(self) -> dict:
        """
        Get a human-readable key identifying the flow

        Returns:
            - dictionary with the keys set as the project identifier,
            flow name, and flow version
        """
        return dict(project=self.project, name=self.name, version=self.version)

    # Context Manager ----------------------------------------------------------

    def __enter__(self) -> "Flow":
        self.__previous_flow = prefect.context.get("_flow")
        prefect.context.update(_flow=self)
        return self

    def __exit__(self, _type, _value, _tb) -> None:  # type: ignore
        del prefect.context._flow
        if self.__previous_flow is not None:
            prefect.context.update(_flow=self.__previous_flow)

        del self.__previous_flow

    # Introspection ------------------------------------------------------------

    def root_tasks(self) -> Set[Task]:
        """
        Get the tasks in the flow that have no upstream dependencies

        Returns:
            - set of Task objects that have no upstream dependencies
        """
        return set(t for t in self.tasks if not self.edges_to(t))

    def terminal_tasks(self) -> Set[Task]:
        """
        Get the tasks in the flow that have no downstream dependencies

        Returns:
            - set of Task objects that have no downstream dependencies
        """
        return set(t for t in self.tasks if not self.edges_from(t))

    def parameters(self, only_required=False) -> Dict[str, ParameterDetails]:
        """
        Get details about any Parameters in this flow

        Args:
            - only_required (bool, optional): Whether or not to only get required parameters

        Returns:
            - dict with format {task name: task} which is a Parameter
        """
        return {
            t.name: {"required": t.required, "default": t.default}
            for t in self.tasks
            if isinstance(t, Parameter) and (t.required if only_required else True)
        }

    def key_tasks(self) -> Set[Task]:
        """
        A flow's "key tasks" are used to determine its state when it runs. If all the key
        tasks are successful, then the flow run is considered successful. However, if
        any of the key tasks fail, the flow is considered to fail. (Note that skips are
        counted as successes.)

        By default, a flow's key tasks are its terminal tasks. This means the state of a
        flow is determined by the last tasks that run.

        In some situations, users may want to customize that behavior; for example, if a
        flow's terminal tasks are "clean up" tasks for the rest of the flow. The
        flow.set_key_tasks() method can be used to set custom key_tasks.

        Please note that even if key_tasks are provided that are not terminal tasks, the flow
        will not be considered "finished" until all terminal tasks have completed. Only then
        will state be determined, using the key tasks.

        Returns:
            - set of Task objects which are the key tasks in the flow
        """
        if self._key_tasks:
            return set(self._key_tasks)
        else:
            return self.terminal_tasks()

    def set_key_tasks(self, tasks: Iterable[Task]) -> None:
        """
        Sets the "key tasks" for the flow. See flow.key_tasks() for more details.

        Args:
            - tasks ([Task]): the tasks that should be set as a flow's key tasks

        Returns:
            - None
        """
        key_tasks = set(tasks)
        if any(t not in self.tasks for t in key_tasks):
            raise ValueError("Key tasks must be part of the flow.")
        self._key_tasks = key_tasks

    # Graph --------------------------------------------------------------------

    def add_task(self, task: Task) -> Task:
        """
        Add a task to the flow if the task does not already exist. The tasks are
        uniquely identified by their `slug`.

        Args:
            - task (Task): the new Task to be added to the flow

        Returns:
            - The Task object passed in if the task was successfully added

        Raises:
            - TypeError: if the the value for `task` is not of type `Task`
            - ValueError: if the `task.slug` matches that of a task in the flow
        """
        if not isinstance(task, Task):
            raise TypeError(
                "Tasks must be Task instances (received {})".format(type(task))
            )
        elif task not in self.tasks:
            if task.slug and task.slug in [t.slug for t in self.tasks]:
                raise ValueError(
                    'A task with the slug "{}" already exists in this '
                    "flow.".format(task.slug)
                )

        self.tasks.add(task)
        self._task_ids[task] = str(uuid.uuid4())

        return task

    def add_edge(
        self,
        upstream_task: Task,
        downstream_task: Task,
        key: str = None,
        validate: bool = None,
    ) -> Edge:
        """
        Add an edge in the flow between two tasks. All edges are directed beginning with
        an upstream task and ending with a downstream task.

        Args:
            - upstream_task (Task): The task that the edge should start from
            - downstream_task (Task): The task that the edge should end with
            - key (str, optional): The key to be set for the new edge
            - validate (bool, optional): Whether or not to check the validity of the flow

        Returns:
            - The Edge object that was successfully added to the flow

        Raises:
            - ValueError: if the `downstream_task` is of type `Parameter`
            - ValueError: if the edge exists with this `key` and `downstream_task`
        """
        if isinstance(downstream_task, Parameter):
            raise ValueError(
                "Parameters must be root tasks and can not have upstream dependencies."
            )

        self.add_task(upstream_task)
        self.add_task(downstream_task)

        # we can only check the downstream task's edges once it has been added to the
        # flow, so we need to perform this check here and not earlier.
        if key and key in {e.key for e in self.edges_to(downstream_task)}:
            raise ValueError(
                'Argument "{a}" for task {t} has already been assigned in '
                "this flow. If you are trying to call the task again with "
                "new arguments, call Task.copy() before adding the result "
                "to this flow.".format(a=key, t=downstream_task)
            )

        edge = Edge(
            upstream_task=upstream_task, downstream_task=downstream_task, key=key
        )
        self.edges.add(edge)

        # check that the edges are valid keywords by binding them
        if key is not None:
            edge_keys = {
                e.key: None for e in self.edges_to(downstream_task) if e.key is not None
            }
            inspect.signature(downstream_task.run).bind_partial(**edge_keys)

        # check for cycles
        if validate is None:
            validate = prefect.config.flows.eager_edge_validation

        if validate:
            self.validate()

        return edge

    def chain(self, *tasks, validate: bool = None) -> List[Edge]:
        """
        Adds a sequence of dependent tasks to the flow.

        Args:
            - *tasks (list): A list of tasks
            - validate (bool, optional): Whether or not to check the validity of the flow

        Returns:
            - A list of Edge objects added to the flow
        """
        edges = []
        for u_task, d_task in zip(tasks, tasks[1:]):
            edges.append(
                self.add_edge(
                    upstream_task=u_task, downstream_task=d_task, validate=validate
                )
            )
        return edges

    def update(self, flow: "Flow", validate: bool = None) -> None:
        """
        Take all tasks and edges in another flow and add it to this flow

        Args:
            - flow (Flow): A flow which is used to update this flow
            - validate (bool, optional): Whether or not to check the validity of the flow

        Returns:
            None
        """
        for task in flow.tasks:
            if task not in self.tasks:
                self.add_task(task)

        for edge in flow.edges:
            if edge not in self.edges:
                self.add_edge(
                    upstream_task=edge.upstream_task,
                    downstream_task=edge.downstream_task,
                    key=edge.key,
                    validate=validate,
                )

    def all_upstream_edges(self) -> Dict[Task, Set[Edge]]:
        """
        Get all of the upstream edges in the flow

        Returns:
            - dict with the key as tasks and the value as a set of upstream edges
        """
        edges = {t: set() for t in self.tasks}  # type: Dict[Task, Set[Edge]]
        for edge in self.edges:
            edges[edge.downstream_task].add(edge)
        return edges

    def all_downstream_edges(self) -> Dict[Task, Set[Edge]]:
        """
        Get all of the downstream edges in the flow

        Returns:
            - dict with the key as tasks and the value as a set of downstream edges
        """
        edges = {t: set() for t in self.tasks}  # type: Dict[Task, Set[Edge]]
        for edge in self.edges:
            edges[edge.upstream_task].add(edge)
        return edges

    def edges_to(self, task: Task) -> Set[Edge]:
        """
        Get all of the edges leading to a task

        Args:
            - task (Task): The task that we want to find edges leading to

        Returns:
            - dict with the key as the task passed in and the value as a set of all edges
            leading to that task

        Raises:
            - ValueError: if `task` is not found in this flow
        """
        if task not in self.tasks:
            raise ValueError(
                "Task {t} was not found in Flow {f}".format(t=task, f=self)
            )
        return self.all_upstream_edges()[task]

    def edges_from(self, task: Task) -> Set[Edge]:
        """
        Get all of the edges leading from a task

        Args:
            - task (Task): The task that we want to find edges leading from

        Returns:
            - dict with the key as the task passed in and the value as a set of all edges
            leading from that task

        Raises:
            - ValueError: if `task` is not found in this flow
        """
        if task not in self.tasks:
            raise ValueError(
                "Task {t} was not found in Flow {f}".format(t=task, f=self)
            )
        return self.all_downstream_edges()[task]

    def upstream_tasks(self, task: Task) -> Set[Task]:
        """
        Get all of the tasks upstream of a task

        Args:
            - task (Task): The task that we want to find upstream tasks of

        Returns:
            - set of Task objects which are upstream of `task`
        """
        return set(e.upstream_task for e in self.edges_to(task))

    def downstream_tasks(self, task: Task) -> Set[Task]:
        """
        Get all of the tasks downstream of a task

        Args:
            - task (Task): The task that we want to find downstream tasks from

        Returns:
            - set of Task objects which are downstream of `task`
        """
        return set(e.downstream_task for e in self.edges_from(task))

    def validate(self) -> None:
        """
        Checks that the flow is valid.

        Raises:
            - ValueError: if edges refer to tasks that are not in this flow
            - ValueError: if specified key tasks are not in this flow
            - ValueError: if any tasks do not have assigned IDs
        """

        if any(e.upstream_task not in self.tasks for e in self.edges) or any(
            e.downstream_task not in self.tasks for e in self.edges
        ):
            raise ValueError("Some edges refer to tasks not contained in this flow.")

        self.sorted_tasks()

        if any(t not in self.tasks for t in self.key_tasks()):
            raise ValueError("Some key tasks are not contained in this flow.")

        if any(t not in self._task_ids for t in self.tasks):
            raise ValueError("Some tasks do not have IDs assigned.")

    def sorted_tasks(self, root_tasks: Iterable[Task] = None) -> Tuple[Task, ...]:
        """
        Get the tasks in this flow in a sorted manner. This allows us to find if any
        cycles exist in this flow's DAG.

        Args:
            - root_tasks ([Tasks], optional): an `Iterable` of `Task` objects

        Returns:
            - tuple of task objects that were sorted

        Raises:
            - ValueError: if a cycle is found in the flow's DAG
        """
        # cache upstream tasks and downstream tasks since we need them often
        upstream_tasks = {
            t: {e.upstream_task for e in edges}
            for t, edges in self.all_upstream_edges().items()
        }
        downstream_tasks = {
            t: {e.downstream_task for e in edges}
            for t, edges in self.all_downstream_edges().items()
        }

        # begin by getting all tasks under consideration (root tasks and all
        # downstream tasks)
        if root_tasks:
            tasks = set(root_tasks)
            seen = set()  # type: Set[Task]

            # while the set of tasks is different from the seen tasks...
            while tasks.difference(seen):
                # iterate over the new tasks...
                for t in list(tasks.difference(seen)):
                    # add its downstream tasks to the task list
                    tasks.update(downstream_tasks[t])
                    # mark it as seen
                    seen.add(t)
        else:
            tasks = self.tasks

        # build the list of sorted tasks
        remaining_tasks = list(tasks)
        sorted_tasks = []
        while remaining_tasks:
            # mark the flow as cyclic unless we prove otherwise
            cyclic = True

            # iterate over each remaining task
            for task in remaining_tasks.copy():
                # check all the upstream tasks of that task
                for upstream_task in upstream_tasks[task]:
                    # if the upstream task is also remaining, it means it
                    # hasn't been sorted, so we can't sort this task either
                    if upstream_task in remaining_tasks:
                        break
                else:
                    # but if all upstream tasks have been sorted, we can sort
                    # this one too. We note that we found no cycle this time.
                    cyclic = False
                    remaining_tasks.remove(task)
                    sorted_tasks.append(task)

            # if we were unable to match any upstream tasks, we have a cycle
            if cyclic:
                raise ValueError("Cycle found; flows must be acyclic!")

        return tuple(sorted_tasks)

    # Dependencies ------------------------------------------------------------

    def set_dependencies(
        self,
        task: object,
        upstream_tasks: Iterable[object] = None,
        downstream_tasks: Iterable[object] = None,
        keyword_tasks: Mapping[str, object] = None,
        validate: bool = None,
    ) -> None:
        """
        Convenience function for adding task dependencies on upstream tasks.

        Args:
            - task (object): a Task that will become part of the Flow. If the task is not a
                Task subclass, Prefect will attempt to convert it to one.
            - upstream_tasks ([object], optional): Tasks that will run before the task runs. If any task
                is not a Task subclass, Prefect will attempt to convert it to one.
            - downstream_tasks ([object], optional): Tasks that will run after the task runs. If any task
                is not a Task subclass, Prefect will attempt to convert it to one.
            - keyword_tasks ({key: object}, optional): The results of these tasks
                will be provided to the task under the specified keyword
                arguments. If any task is not a Task subclass, Prefect will attempt to
                convert it to one.
            - validate (bool, optional): Whether or not to check the validity of the flow

        Returns:
            None
        """

        task = as_task(task)
        assert isinstance(task, Task)  # mypy assert

        # add the main task (in case it was called with no arguments)
        self.add_task(task)

        # add upstream tasks
        for t in upstream_tasks or []:
            t = as_task(t)
            assert isinstance(t, Task)  # mypy assert
            self.add_edge(upstream_task=t, downstream_task=task, validate=validate)

        # add downstream tasks
        for t in downstream_tasks or []:
            t = as_task(t)
            assert isinstance(t, Task)  # mypy assert
            self.add_edge(upstream_task=task, downstream_task=t, validate=validate)

        # add data edges to upstream tasks
        for key, t in (keyword_tasks or {}).items():
            t = as_task(t)
            assert isinstance(t, Task)  # mypy assert
            self.add_edge(
                upstream_task=t, downstream_task=task, key=key, validate=validate
            )

    # Execution  ---------------------------------------------------------------

    def run(
        self,
        parameters: Dict[str, Any] = None,
        return_tasks: Iterable[Task] = None,
        **kwargs
    ) -> "prefect.engine.state.State":
        """
        Run the flow using an instance of a FlowRunner

        Args:
            - parameters (Dict[str, Any], optional): values to pass into the runner
            - return_tasks ([Task], optional): list of tasks which return state
            - **kwargs: additional keyword arguments; if any provided keywords
                match known parameter names, they will be used as such. Otherwise they will be passed to the
                `FlowRunner.run()` method

        Returns:
            - State of the flow after it is run resulting from it's return tasks
        """
        runner = prefect.engine.flow_runner.FlowRunner(flow=self)
        parameters = parameters or []

        passed_parameters = {}
        for p in self.parameters():
            if p in kwargs:
                passed_parameters[p] = kwargs.pop(p)
            elif p in parameters:
                passed_parameters[p] = parameters[p]

        state = runner.run(
            parameters=passed_parameters, return_tasks=return_tasks, **kwargs
        )

        # state always should return a dict of tasks. If it's None (meaning the run was
        # interrupted before any tasks were executed), we set the dict manually.
        if state.result is None:
            state.result = {}
        for task in return_tasks or []:
            if task not in state.result:
                state.result[task] = prefect.engine.state.Pending(
                    message="Task not run."
                )
        return state

    # Visualization ------------------------------------------------------------

    def visualize(self):
        """
        Creates graphviz object for representing the current flow
        """

        graph = graphviz.Digraph()

        for t in self.tasks:
            graph.node(str(id(t)), t.name)

        for e in self.edges:
            graph.edge(str(id(e.upstream_task)), str(id(e.downstream_task)), e.key)

        try:
            if get_ipython().config.get("IPKernelApp") is not None:
                return graph
        except NameError:
            pass

        with tempfile.NamedTemporaryFile() as tmp:
            graph.render(tmp.name, view=True)

    # Building / Serialization ----------------------------------------------------

    def serialize(self, build=False) -> dict:
        """
        Creates a serialized representation of the flow.

        Args:
            - build (bool, optional): if `True`, the flow's environment is built and the resulting
                `environment_key` is included in the serialized flow. If `False` (default),
                the environment is not build and the `environment_key` is `None`.

        Returns:
            - dict representing the flow
        """

        local_task_ids = self.generate_local_task_ids()
        if self.environment and build:
            environment_key = self.build_environment()
        else:
            environment_key = None

        return dict(
            id=self.id,
            name=self.name,
            version=self.version,
            project=self.project,
            description=self.description,
            environment=self.environment,
            environment_key=environment_key,
            parameters=self.parameters(),
            schedule=self.schedule,
            tasks={
                self._task_ids[t]: dict(
                    id=self._task_ids[t], local_id=local_task_ids[t], **t.serialize()
                )
                for t in self.tasks
            },
            key_tasks=[self._task_ids[t] for t in self.key_tasks()],
            edges=[
                dict(
                    upstream_task_id=self._task_ids[e.upstream_task],
                    downstream_task_id=self._task_ids[e.downstream_task],
                    key=e.key,
                )
                for e in self.edges
            ],
        )

    def register(self) -> None:
        """Register the flow."""
        return prefect.core.registry.register_flow(self)

    def build_environment(self) -> bytes:
        """
        Build the flow's environment.

        Returns:
            - bytes of a key that can be used to access the environment.

        Raises:
            - ValueError: if no environment is specified in this flow
        """
        if not self.environment:
            raise ValueError("No environment set!")
        return self.environment.build(self)

    def generate_local_task_ids(
        self, *, _debug_steps: bool = False
    ) -> Dict[str, "Task"]:
        """
        Generates stable IDs for each task that track across flow versions

        If our goal was to create an ID for each task, we would simply produce a random
        hash. However, we would prefer to generate deterministic IDs. That way, identical
        flows will have the same task ids and near-identical flows will have overlapping
        task ids.

        If all tasks were unique, we could simply produce unique IDs by hashing the tasks
        themselves. However, Prefect allows duplicate tasks in a flow. Therefore, we take a
        few steps to iteratively produce unique IDs. There are five steps, and tasks go
        through each step until they have a unique ID:

            1. Generate an ID from the task's attributes.
                This fingerprints a task in terms of its own attributes.
            2. Generate an ID from the task's ancestors.
                This fingerprints a task in terms of the computational graph leading to it.
            3. Generate an ID from the task's descendents
                This fingerprints a task in terms of how it is used in a computational graph.
            4. Iteratively generate an ID from the task's neighbors
                This fingerprints a task in terms of a widening concentric circle of its neighbors.
            5. Adjust a root task's ID and recompute all non-unique descendents
                This step is only reached if a flow contains more than one unconnected but
                identical computational paths. The previous 4 steps are unable to distinguish
                between those two paths, so we pick one at random and adjust the leading tasks'
                IDs, as well as all following tasks. This is safe because we are sure that the
                computational paths are identical.

        Args:
            - flow (Flow)
            - _debug_steps (bool, optional): if True, the function will return a dictionary of
                {step_number: ids_produced_at_step} pairs, where ids_produced_at_step is the
                id dict following that step. This is used for debugging/testing only.

        Returns:
            - dict: a dictionary of {task: task_id} pairs
        """

        # precompute flow properties since we'll need to access them repeatedly
        tasks = self.sorted_tasks()
        edges_to = self.all_upstream_edges()
        edges_from = self.all_downstream_edges()

        # dictionary to hold debug information
        debug_steps = {}

        # -- Step 1 ---------------------------------------------------
        #
        # Generate an ID for each task by hashing:
        # - its serialized version
        # - its flow's project
        # - its flow's name
        #
        # This "fingerprints" each task in terms of its own characteristics and the parent flow.
        # Note that the fingerprint does not include the flow version, meaning task IDs can
        # remain stable across versions of the same flow.
        #
        # -----------------------------------------------------------

        ids = {
            t: _hash(dumps((t.serialize(), self.project, self.name), sort_keys=True))
            for t in tasks
        }

        if _debug_steps:
            debug_steps[1] = ids.copy()

        # -- Step 2 ---------------------------------------------------
        #
        # Next, we iterate over the tasks in topological order and, for any task without
        # a unique ID, produce a new ID based on its current ID and the ID of any
        # upstream nodes. This fingerprints each task in terms of all its ancestors.
        #
        # -----------------------------------------------------------

        counter = Counter(ids.values())
        for task in tasks:
            if counter[ids[task]] == 1:
                continue

            # create a new id by hashing (task id, upstream edges, downstream edges)
            edges = sorted((e.key, ids[e.upstream_task]) for e in edges_to[task])
            ids[task] = _hash(str((ids[task], edges)))

        if _debug_steps:
            debug_steps[2] = ids.copy()

        # -- Step 3 ---------------------------------------------------
        #
        # Next, we iterate over the tasks in reverse topological order and, for any task
        # without a unique ID, produce a new ID based on its current ID and the ID of
        # any downstream nodes. After this step, each task is fingerprinted by its
        # position in a computational chain (both ancestors and descendents).
        #
        # -----------------------------------------------------------

        counter = Counter(ids.values())
        for task in reversed(tasks):
            if counter[ids[task]] == 1:
                continue

            # create a new id by hashing (task id, upstream edges, downstream edges)
            edges = sorted((e.key, ids[e.downstream_task]) for e in edges_from[task])
            ids[task] = _hash(str((ids[task], edges)))

        if _debug_steps:
            debug_steps[3] = ids.copy()

        # -- Step 4 ---------------------------------------------------
        #
        # It is still possible for tasks to have duplicate IDs. For example, the
        # following flow of identical tasks would not be able to differentiate between
        # y3 and z3 after a forward and backward pass.
        #
        #               x1 -> x2 -> x3 -> x4
        #                  \
        #               y1 -> y2 -> y3 -> y4
        #                  \
        #               z1 -> z2 -> z3 -> z4
        #
        # We could continue running forward and backward passes to diffuse task
        # dependencies through the graph, but that approach is inefficient and
        # introduces very long dependency chains. Instead, we take each task and produce
        # a new ID by hashing it with the IDs of all of its upstream and downstream
        # neighbors.
        #
        # Each time we repeat this step, the non-unique task ID incorporates information
        # from tasks farther and farther away, because its neighbors are also updating
        # their IDs from their own neighbors. (note that we could use this algorithm
        # exclusively, but starting with a full forwards and backwards pass is much
        # faster!)
        #
        # However, it is still possible for this step to fail to generate a unique ID
        # for every task. The simplest example of this case is a flow with two
        # unconnected but identical tasks; the algorithm will be unable to differentiate
        # between the two based solely on their neighbors.
        #
        # Therefore, we continue updating IDs in this step only until the number of
        # unique IDs stops increasing. At that point, any remaining duplicates can not
        # be distinguished on the basis of neighboring nodes.
        #
        # -----------------------------------------------------------

        counter = Counter(ids.values())

        # continue this algorithm as long as the number of unique ids keeps changing
        while True:

            # store the number of unique ids at the beginning of the loop
            starting_unique_id_count = len(counter)

            for task in tasks:

                # if the task already has a unique id, just go to the next one
                if counter[ids[task]] == 1:
                    continue

                # create a new id by hashing the task ID with upstream dn downstream IDs
                edges = [
                    sorted((e.key, ids[e.upstream_task]) for e in edges_to[task]),
                    sorted((e.key, ids[e.downstream_task]) for e in edges_from[task]),
                ]
                ids[task] = _hash(str((ids[task], edges)))

            # recompute a new counter.
            # note: we can't do this incremenetally because we can't guarantee the
            # iteration order, and incremental updates would implicitly depend on order
            counter = Counter(ids.values())

            # if the new counter has the same number of unique IDs as the old counter,
            # then the algorithm is no longer able to produce useful ids
            if len(counter) == starting_unique_id_count:
                break

        if _debug_steps:
            debug_steps[4] = ids.copy()

        # -- Step 5 ---------------------------------------------------
        #
        # If the number of unique IDs is less than the number of tasks at this stage, it
        # means that the algorithm in step 4 was unable to differentiate between some
        # tasks. This is only possible if the self contains identical but unconnected
        # computational paths.
        #
        # To remedy this, we change the ids of the duplicated root tasks until they are
        # unique, then recompute the ids of all downstream tasks. While this chooses the
        # affected root task at random, we are confident that the tasks are exact
        # duplicates so this is of no consequence.
        #
        # -----------------------------------------------------------

        while len(counter) < len(tasks):
            for task in tasks:
                # recompute each task's ID until it is unique
                while counter[ids[task]] != 1:
                    edges = sorted(
                        (e.key, ids[e.upstream_task]) for e in edges_to[task]
                    )
                    ids[task] = _hash(str((ids[task], edges)))
                    counter[ids[task]] += 1

        if _debug_steps:
            debug_steps[5] = ids.copy()
            return debug_steps

        return ids


def _hash(value):
    return xxhash.xxh64(value).digest()