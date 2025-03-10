// Copyright (C) 2021-2022 Intel Corporation
// Copyright (C) 2023 CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import _ from 'lodash';
import {
    useRef, useEffect, useState, useCallback,
} from 'react';
import { useSelector } from 'react-redux';
import { CombinedState, PluginComponent } from 'reducers';

// eslint-disable-next-line import/prefer-default-export
export function usePrevious<T>(value: T): T | undefined {
    const ref = useRef<T>();
    useEffect(() => {
        ref.current = value;
    });
    return ref.current;
}

export function useIsMounted(): () => boolean {
    const ref = useRef(false);

    useEffect(() => {
        ref.current = true;
        return () => {
            ref.current = false;
        };
    }, []);

    return useCallback(() => ref.current, []);
}

type Plugin = {
    component: CallableFunction;
    weight: number;
};

export function usePlugins(
    getState: (state: CombinedState) => PluginComponent[],
    props: object = {}, state: object = {},
): Plugin[] {
    const components = useSelector(getState);
    const filteredComponents = components.filter((component) => component.data.shouldBeRendered(props, state));
    const mappedComponents = filteredComponents
        .map(({ component, data }): {
            component: CallableFunction;
            weight: number;
        } => ({
            component,
            weight: data.weight,
        }));
    const ref = useRef<Plugin[]>(mappedComponents);

    if (!_.isEqual(ref.current, mappedComponents)) {
        ref.current = mappedComponents;
    }

    return ref.current;
}

export interface ICardHeightHOC {
    numberOfRows: number;
    paddings: number;
    containerClassName: string;
    siblingClassNames: string[];
}

export function useCardHeightHOC(params: ICardHeightHOC): () => string {
    const {
        numberOfRows, paddings, containerClassName, siblingClassNames,
    } = params;

    return (): string => {
        const [height, setHeight] = useState('auto');
        useEffect(() => {
            const resize = (): void => {
                const container = window.document.getElementsByClassName(containerClassName)[0];
                const siblings = siblingClassNames.map(
                    (classname: string): Element | undefined => window.document.getElementsByClassName(classname)[0],
                );

                if (container) {
                    const { clientHeight: containerHeight } = container;
                    const othersHeight = siblings.reduce<number>((acc: number, el: Element | undefined): number => {
                        if (el) {
                            return acc + el.clientHeight;
                        }

                        return acc;
                    }, 0);

                    const cardHeight = (containerHeight - (othersHeight + paddings)) / numberOfRows;
                    setHeight(`${Math.round(cardHeight)}px`);
                }
            };

            resize();
            window.addEventListener('resize', resize);

            return () => {
                window.removeEventListener('resize', resize);
            };
        }, []);

        return height;
    };
}
