import { useEffect, useState } from 'react';
import { fetchTask } from '../api';
import type { Task } from '../types';

/** Lists tasks. */
export default function TaskList() {
  const [task, setTask] = useState<Task | null>(null);
  useEffect(() => {
    fetchTask(1).then(setTask);
  }, []);
  return <div>{task?.title}</div>;
}
