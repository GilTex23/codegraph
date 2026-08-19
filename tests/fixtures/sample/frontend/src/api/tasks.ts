import { client } from './client';
import type { Task } from '../types';

/** Load one task by id. */
export async function fetchTask(id: number): Promise<Task> {
  return client.get(`/api/v1/tasks/${id}`);
}

export async function createTask(body: unknown): Promise<Task> {
  return client.post('/api/v1/tasks', body);
}
