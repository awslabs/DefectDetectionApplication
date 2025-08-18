/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

// Helper file to interact with react-query

import { QueryClient } from "@tanstack/react-query";

export const createQueryClient = (): QueryClient =>
  new QueryClient({
    defaultOptions: {
      queries: {
        // Setting staleTime to Infinity as a default means that queries will
        // never be refetched automatically. To refetch, you need to explicitly
        // call refetch on the query, call invalidateQueries on the queryClient,
        // or (of course) refresh the page. I think this works for most of our
        // current queries. You can always configure staleTime further on an
        // individual query.
        staleTime: Infinity,
      },
    },
  });
